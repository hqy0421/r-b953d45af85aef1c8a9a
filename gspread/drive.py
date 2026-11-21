"""
gspread.drive
~~~~~~~~~~~~~

Helpers to collect spreadsheets from a Google Drive folder tree.

The Drive API exposes folders as files, and represents "shortcuts" as files of
type ``application/vnd.google-apps.shortcut`` pointing at a target file, which
can itself be a folder or a spreadsheet.  This module walks such a tree
breadth-first, follows shortcuts, and reports the canonical path of every
accessible spreadsheet.

The traversal is deterministic:

* files and directories are deduplicated by their *real* target ID, so a
  folder reached through several entries is listed only once, and a
  spreadsheet reached through several paths is reported once;
* the reported path is the one with the fewest segments; ties are broken by
  comparing each segment's name then ID, in order;
* a path segment created by a shortcut keeps the shortcut's name, while its ID
  is the shortcut target's real ID.

Shortcut targets that were deleted (or trashed) or that the authenticated user
cannot view are reported separately in
:attr:`~gspread.drive.SpreadsheetCollection.shortcut_failures`, each with a
distinct reason; they are never reported as collected spreadsheets.  Other
request errors are recorded in
:attr:`~gspread.drive.SpreadsheetCollection.errors`; they make
:attr:`~gspread.drive.SpreadsheetCollection.complete` ``False`` so a caller can
tell that the listing of the current tree is incomplete.
"""

import heapq
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple

from .exceptions import APIError, InvalidInputValue
from .http_client import HTTPClient, ParamsType
from .urls import DRIVE_FILES_API_V3_URL, SPREADSHEET_DRIVE_URL
from .utils import MimeType, StrEnum

FOLDER_MIME_TYPE = MimeType.folder
SHORTCUT_MIME_TYPE = MimeType.shortcut

_FOLDER_URL_RE = re.compile(r"/folders/([A-Za-z0-9_-]+)")
_FOLDER_ID_PARAM_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]+)")
_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# ``shortcutDetails.targetMimeType`` is requested only to make the payload
# self-describing; it is never trusted, because it can be stale.
_LIST_FIELDS = (
    "nextPageToken," "files(id,name,mimeType,shortcutDetails(targetId,targetMimeType))"
)
_GET_FIELDS = "id,name,mimeType,trashed,shortcutDetails(targetId,targetMimeType)"
_PAGE_SIZE = 1000


class ShortcutFailureReason(StrEnum):
    """Why a shortcut target could not be collected."""

    deleted = "deleted"
    permission_denied = "permission_denied"


@dataclass(frozen=True)
class PathSegment:
    """One step of a path, from the entry folder down to a file.

    ``id`` is always a *real* Drive ID: for a segment created by a shortcut it
    is the shortcut target's ID, while ``name`` is the shortcut's name.
    """

    id: str
    name: str


@dataclass(frozen=True)
class CollectedSpreadsheet:
    """An accessible spreadsheet found while walking a folder tree.

    :param str id: the spreadsheet's real (target) ID
    :param str name: the spreadsheet's actual title
    :param str mime_type: the target's actual MIME type
    :param tuple path: the canonical path from the entry folder to the
        spreadsheet, as :class:`PathSegment` objects.  The last segment is the
        spreadsheet itself and keeps the name it was reached through, which is
        the shortcut's name when it was reached through a shortcut.
    """

    id: str
    name: str
    mime_type: str
    path: Tuple[PathSegment, ...]

    @property
    def url(self) -> str:
        """The spreadsheet's Google Sheets URL."""
        return SPREADSHEET_DRIVE_URL % self.id

    @property
    def display_path(self) -> str:
        """The canonical path as ``"Root/Sub folder/My sheet"``."""
        return "/".join(segment.name for segment in self.path)


@dataclass(frozen=True)
class ShortcutFailure:
    """A shortcut whose target is deleted or not viewable.

    ``target_id`` is the target that could not be read; when the shortcut
    points through a chain, this is the link in the chain that failed.
    """

    shortcut_id: str
    shortcut_name: str
    target_id: Optional[str]
    reason: ShortcutFailureReason
    status_code: Optional[int]
    path: Tuple[PathSegment, ...]


@dataclass(frozen=True)
class CollectionError:
    """A request error that may leave the traversal incomplete.

    ``operation`` is either ``"list_folder"`` (a page of a directory listing
    failed) or ``"resolve_shortcut"`` (a shortcut target could not be read for
    a reason other than deletion or missing permission).
    """

    operation: str
    message: str
    status_code: Optional[int]
    path: Tuple[PathSegment, ...]
    folder_id: Optional[str] = None
    shortcut_id: Optional[str] = None


@dataclass(frozen=True)
class DirectoryRecord:
    """A directory that was visited exactly once during the traversal."""

    id: str
    name: str
    path: Tuple[PathSegment, ...]
    error: Optional[CollectionError] = None

    @property
    def complete(self) -> bool:
        """Whether every page of this directory's listing was read."""
        return self.error is None


@dataclass
class SpreadsheetCollection:
    """The outcome of :meth:`gspread.Client.collect_spreadsheets`.

    :ivar spreadsheets: every accessible spreadsheet, deduplicated by real ID
    :ivar shortcut_failures: shortcuts skipped because their target is deleted
        or not viewable
    :ivar errors: other request errors encountered during the traversal
    :ivar directories: the directories visited, each with its listing status
    """

    spreadsheets: List[CollectedSpreadsheet] = field(default_factory=list)
    shortcut_failures: List[ShortcutFailure] = field(default_factory=list)
    errors: List[CollectionError] = field(default_factory=list)
    directories: List[DirectoryRecord] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """``True`` when no request failed unexpectedly.

        Deleted or unreadable shortcut targets do not make a traversal
        incomplete: they are expected findings, reported through
        :attr:`shortcut_failures`.
        """
        return not self.errors

    @property
    def ids(self) -> List[str]:
        """The real IDs of all collected spreadsheets."""
        return [spreadsheet.id for spreadsheet in self.spreadsheets]

    @property
    def incomplete_directories(self) -> List[DirectoryRecord]:
        """Directories whose contents could not be fully listed."""
        return [directory for directory in self.directories if not directory.complete]

    @property
    def unresolved_shortcuts(self) -> List[CollectionError]:
        """Shortcut lookups that failed for an unexpected reason."""
        return [error for error in self.errors if error.operation == "resolve_shortcut"]

    def __iter__(self):
        return iter(self.spreadsheets)

    def __len__(self) -> int:
        return len(self.spreadsheets)

    def __getitem__(self, index):
        return self.spreadsheets[index]


class _TargetDeleted(Exception):
    def __init__(self, status_code: Optional[int], target_id: str) -> None:
        super().__init__("shortcut target is deleted")
        self.status_code = status_code
        self.target_id = target_id


class _TargetForbidden(Exception):
    def __init__(self, status_code: Optional[int], target_id: str) -> None:
        super().__init__("shortcut target is not accessible")
        self.status_code = status_code
        self.target_id = target_id


class _MalformedShortcut(Exception):
    pass


def _path_key(path: Tuple[PathSegment, ...]) -> Tuple[int, Tuple[Tuple[str, str], ...]]:
    """Order paths by segment count, then by each segment's name then ID."""
    return (
        len(path),
        tuple((segment.name, segment.id) for segment in path),
    )


def _segment(item: Mapping[str, Any], real_id: Optional[str] = None) -> PathSegment:
    return PathSegment(
        id=str(real_id if real_id is not None else item.get("id", "")),
        name=str(item.get("name") or ""),
    )


def _status_code(error: APIError) -> Optional[int]:
    response = getattr(error, "response", None)
    if response is not None:
        return response.status_code
    return getattr(error, "code", None)


def _error_message(error: APIError) -> str:
    details = getattr(error, "error", None) or {}
    message = details.get("message") if isinstance(details, Mapping) else None
    return str(message or error)


# ``errors[].reason`` values Drive uses for targets that are gone or that the
# caller may not view.  The HTTP status alone cannot tell them apart: Drive
# answers 404 for both a deleted file and one shared with someone else.
_DELETED_REASONS = {"notFound", "fileNotFound"}
_FORBIDDEN_REASONS = {
    "forbidden",
    "insufficientFilePermissions",
    "cannotAccessFile",
    "cannotListParentOfRootException",
}


def _failure_reason(error: APIError) -> Optional[ShortcutFailureReason]:
    """Classify a failed metadata lookup as deleted/no access, if possible."""
    details = getattr(error, "error", None)
    reasons = set()
    if isinstance(details, Mapping):
        errors = details.get("errors")
        if isinstance(errors, list):
            reasons = {
                str(item.get("reason")) for item in errors if isinstance(item, Mapping)
            }
    if reasons & _FORBIDDEN_REASONS:
        return ShortcutFailureReason.permission_denied
    if reasons & _DELETED_REASONS:
        return ShortcutFailureReason.deleted
    return None


def _escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def extract_folder_id(folder: str) -> str:
    """Return a folder ID from a raw ID or a Drive folder URL.

    >>> extract_folder_id("1abcDEF")
    '1abcDEF'

    >>> extract_folder_id("https://drive.google.com/drive/folders/1abcDEF?usp=sharing")
    '1abcDEF'

    >>> extract_folder_id("https://drive.google.com/open?id=1abcDEF")
    '1abcDEF'

    >>> extract_folder_id("https://example.com/not-a-folder")
    Traceback (most recent call last):
        ...
    gspread.exceptions.InvalidInputValue: no folder ID found in 'https://example.com/not-a-folder'
    """
    match = _FOLDER_URL_RE.search(folder)
    if match:
        return match.group(1)

    match = _FOLDER_ID_PARAM_RE.search(folder)
    if match:
        return match.group(1)

    candidate = folder.strip()
    if _FOLDER_ID_RE.match(candidate):
        return candidate

    raise InvalidInputValue("no folder ID found in {!r}".format(folder))


class _FolderWalker:
    def __init__(self, http_client: HTTPClient) -> None:
        self.http_client = http_client
        # target id -> ("ok", metadata) | ("deleted", None) | ("forbidden", None)
        self._targets: Dict[str, Tuple[str, Optional[Dict[str, Any]]]] = {}

    def _get_json(self, url: str, params: ParamsType) -> Dict[str, Any]:
        return self.http_client.request("get", url, params=params).json()

    def _file_metadata(self, file_id: str) -> Dict[str, Any]:
        url = "{}/{}".format(DRIVE_FILES_API_V3_URL, file_id)
        params: ParamsType = {"supportsAllDrives": True, "fields": _GET_FIELDS}
        return self._get_json(url, params)

    def _entry_folder(self, folder_id: str) -> Tuple[str, str]:
        """Resolve the starting folder to ``(real_id, name)``.

        The entry may itself be a shortcut, in which case its chain is
        followed.  Errors here propagate as :class:`~gspread.exceptions.APIError`
        because without the entry folder there is nothing to report on.
        """
        metadata = self._file_metadata(folder_id)
        seen = {folder_id}
        while metadata.get("mimeType") == SHORTCUT_MIME_TYPE:
            target_id = (metadata.get("shortcutDetails") or {}).get("targetId")
            if not target_id or target_id in seen:
                raise InvalidInputValue(
                    "entry shortcut {!r} has no usable target".format(folder_id)
                )
            seen.add(target_id)
            metadata = self._file_metadata(str(target_id))
            folder_id = str(target_id)

        if metadata.get("trashed"):
            raise InvalidInputValue("Drive item {!r} is in the trash".format(folder_id))

        if metadata.get("mimeType") != FOLDER_MIME_TYPE:
            raise InvalidInputValue(
                "Drive item {!r} is not a folder ({})".format(
                    folder_id, metadata.get("mimeType")
                )
            )

        return folder_id, str(metadata.get("name") or "")

    def _list_children(
        self, folder_id: str
    ) -> Tuple[List[Dict[str, Any]], Optional[APIError]]:
        """List a folder page by page.

        Returns the files read so far together with the error that interrupted
        the listing, if any.  Partial results are kept so a single failing page
        does not discard the pages before it.
        """
        params: ParamsType = {
            "q": "'{}' in parents and trashed = false".format(_escape_query(folder_id)),
            "pageSize": _PAGE_SIZE,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
            "fields": _LIST_FIELDS,
        }
        files: List[Dict[str, Any]] = []
        page_token = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            try:
                payload = self._get_json(DRIVE_FILES_API_V3_URL, params)
            except APIError as error:
                return files, error
            files.extend(payload.get("files", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return files, None

    def _target_metadata(self, target_id: str) -> Dict[str, Any]:
        """Read a shortcut target, translating "gone" and "no access" errors."""
        cached = self._targets.get(target_id)
        if cached is not None:
            status, metadata = cached
            if status == "deleted":
                raise _TargetDeleted(HTTPStatus.NOT_FOUND, target_id)
            if status == "forbidden":
                raise _TargetForbidden(HTTPStatus.FORBIDDEN, target_id)
            return metadata or {}

        try:
            metadata = self._file_metadata(target_id)
        except APIError as error:
            code = _status_code(error)
            reason = _failure_reason(error)
            if reason is ShortcutFailureReason.permission_denied:
                self._targets[target_id] = ("forbidden", None)
                raise _TargetForbidden(code, target_id)
            if reason is ShortcutFailureReason.deleted or code in (
                HTTPStatus.NOT_FOUND,
                HTTPStatus.GONE,
            ):
                self._targets[target_id] = ("deleted", None)
                raise _TargetDeleted(code, target_id)
            if code == HTTPStatus.FORBIDDEN:
                self._targets[target_id] = ("forbidden", None)
                raise _TargetForbidden(code, target_id)
            raise

        if metadata.get("trashed"):
            self._targets[target_id] = ("deleted", None)
            raise _TargetDeleted(HTTPStatus.NOT_FOUND, target_id)

        self._targets[target_id] = ("ok", metadata)
        return metadata

    def _resolve(self, item: Mapping[str, Any]) -> Tuple[str, str, str]:
        """Return ``(real_id, actual_mime_type, actual_name)`` for a listing entry.

        Shortcut chains are followed.  The target's actual metadata is always
        fetched, so a stale ``shortcutDetails.targetMimeType`` hint never
        decides whether an item is collected or traversed.
        """
        mime_type = item.get("mimeType")
        item_id = str(item.get("id", ""))
        if mime_type != SHORTCUT_MIME_TYPE:
            return item_id, str(mime_type or ""), str(item.get("name") or "")

        shortcut_name = str(item.get("name") or "")
        details = item.get("shortcutDetails") or {}
        target_id = details.get("targetId")
        if not target_id:
            raise _MalformedShortcut(
                "shortcut {!r} has no targetId".format(item_id or shortcut_name)
            )

        seen = {item_id} if item_id else set()
        while True:
            target_id = str(target_id)
            if target_id in seen:
                raise _MalformedShortcut(
                    "shortcut {!r} points back to itself".format(
                        item_id or shortcut_name
                    )
                )
            seen.add(target_id)

            metadata = self._target_metadata(target_id)
            target_mime = metadata.get("mimeType")
            if target_mime != SHORTCUT_MIME_TYPE:
                return (
                    target_id,
                    str(target_mime or ""),
                    str(metadata.get("name") or shortcut_name),
                )

            target_id = (metadata.get("shortcutDetails") or {}).get("targetId")
            if not target_id:
                raise _MalformedShortcut(
                    "shortcut chain from {!r} has no targetId".format(
                        item_id or shortcut_name
                    )
                )

    def walk(self, folder: str) -> SpreadsheetCollection:
        entry_id = extract_folder_id(folder)
        root_id, root_name = self._entry_folder(entry_id)

        result = SpreadsheetCollection()
        root_path = (PathSegment(id=root_id, name=root_name),)

        # Priorities are path keys: fewer segments first, then the per-segment
        # name/ID comparison.  A child's key is always greater than its
        # parent's, so the first time a directory is popped it carries its
        # canonical path; that makes it safe to list each one exactly once.
        heap = [(_path_key(root_path), 0, root_id, root_path)]
        counter = 1
        settled = set()  # real folder IDs, each listed at most once
        spreadsheets: Dict[str, CollectedSpreadsheet] = {}
        failures: Dict[str, ShortcutFailure] = {}

        while heap:
            _, _, folder_id, path = heapq.heappop(heap)
            if folder_id in settled:
                continue
            settled.add(folder_id)

            children, list_error = self._list_children(folder_id)
            collection_error = None
            if list_error is not None:
                collection_error = CollectionError(
                    operation="list_folder",
                    message=_error_message(list_error),
                    status_code=_status_code(list_error),
                    path=path,
                    folder_id=folder_id,
                )
                result.errors.append(collection_error)

            result.directories.append(
                DirectoryRecord(
                    id=folder_id,
                    name=path[-1].name,
                    path=path,
                    error=collection_error,
                )
            )

            for item in children:
                self._visit(
                    item,
                    path,
                    result,
                    spreadsheets,
                    failures,
                    heap,
                    counter,
                )
                counter += 1

        result.spreadsheets = sorted(
            spreadsheets.values(), key=lambda record: _path_key(record.path)
        )
        result.shortcut_failures = sorted(
            failures.values(), key=lambda failure: _path_key(failure.path)
        )
        result.errors.sort(key=lambda error: _path_key(error.path))
        result.directories.sort(key=lambda directory: _path_key(directory.path))
        return result

    def _visit(
        self,
        item: Mapping[str, Any],
        path: Tuple[PathSegment, ...],
        result: SpreadsheetCollection,
        spreadsheets: Dict[str, CollectedSpreadsheet],
        failures: Dict[str, ShortcutFailure],
        heap: List[Any],
        counter: int,
    ) -> None:
        name = str(item.get("name") or "")
        item_id = str(item.get("id", ""))
        try:
            target_id, target_mime, actual_name = self._resolve(item)
        except _TargetDeleted as gone:
            self._record_failure(
                failures,
                item,
                item_id,
                name,
                path,
                ShortcutFailureReason.deleted,
                gone.status_code,
                gone.target_id,
            )
            return
        except _TargetForbidden as forbidden:
            self._record_failure(
                failures,
                item,
                item_id,
                name,
                path,
                ShortcutFailureReason.permission_denied,
                forbidden.status_code,
                forbidden.target_id,
            )
            return
        except _MalformedShortcut as malformed:
            self._record_shortcut_error(result, item, item_id, path, str(malformed))
            return
        except APIError as error:
            self._record_shortcut_error(
                result,
                item,
                item_id,
                path,
                _error_message(error),
                _status_code(error),
            )
            return

        child_path = path + (_segment(item, real_id=target_id),)

        if target_mime == FOLDER_MIME_TYPE:
            heapq.heappush(
                heap,
                (_path_key(child_path), counter, target_id, child_path),
            )
            return

        if target_mime != MimeType.google_sheets:
            # Other file types (docs, PDFs, ...) are not of interest.
            return

        record = CollectedSpreadsheet(
            id=target_id,
            name=actual_name or name,
            mime_type=target_mime,
            path=child_path,
        )
        current = spreadsheets.get(target_id)
        if current is None or _path_key(record.path) < _path_key(current.path):
            spreadsheets[target_id] = record

    @staticmethod
    def _record_shortcut_error(
        result: SpreadsheetCollection,
        item: Mapping[str, Any],
        item_id: str,
        path: Tuple[PathSegment, ...],
        message: str,
        status_code: Optional[int] = None,
    ) -> None:
        target_id = (item.get("shortcutDetails") or {}).get("targetId")
        leaf = PathSegment(
            id=str(target_id or item_id), name=str(item.get("name") or "")
        )
        result.errors.append(
            CollectionError(
                operation="resolve_shortcut",
                message=message,
                status_code=status_code,
                path=path + (leaf,),
                folder_id=path[-1].id,
                shortcut_id=item_id or None,
            )
        )

    @staticmethod
    def _record_failure(
        failures: Dict[str, ShortcutFailure],
        item: Mapping[str, Any],
        item_id: str,
        name: str,
        path: Tuple[PathSegment, ...],
        reason: ShortcutFailureReason,
        status_code: Optional[int],
        target_id: Optional[str] = None,
    ) -> None:
        direct_target = (item.get("shortcutDetails") or {}).get("targetId")
        leaf_id = str(target_id or direct_target or item_id)
        failure = ShortcutFailure(
            shortcut_id=item_id,
            shortcut_name=name,
            target_id=leaf_id,
            reason=reason,
            status_code=status_code,
            path=path + (PathSegment(id=leaf_id, name=name),),
        )
        current = failures.get(item_id)
        if current is None or _path_key(failure.path) < _path_key(current.path):
            failures[item_id] = failure


def collect_spreadsheets(http_client: HTTPClient, folder: str) -> SpreadsheetCollection:
    """Collect spreadsheets from ``folder`` and its sub-folders.

    This is the implementation behind
    :meth:`gspread.Client.collect_spreadsheets`; see that method for details.
    """
    return _FolderWalker(http_client).walk(folder)
