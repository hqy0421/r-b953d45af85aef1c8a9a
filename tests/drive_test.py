import unittest
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Union, cast

from requests import Response

import gspread
from gspread.client import Client
from gspread.drive import ShortcutFailureReason, extract_folder_id
from gspread.exceptions import APIError, InvalidInputValue
from gspread.http_client import HTTPClient, HTTPClientType, ParamsType
from gspread.urls import DRIVE_FILES_API_V3_URL
from gspread.utils import MimeType

FOLDER = MimeType.folder
SHEET = MimeType.google_sheets
SHORTCUT = MimeType.shortcut

ENTRY = "entry"
OTHER = "other"


class FakeResponse:
    def __init__(self, body: Any, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.text = str(body)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        return self._body


def as_response(response: FakeResponse) -> Response:
    return cast(Response, response)


class FakeDrive(HTTPClient):
    """An in-memory stand-in for the two Drive endpoints the walker uses."""

    def __init__(self, entries: Dict[str, Dict[str, Any]], page_size: int = 1) -> None:
        # does not call HTTPClient.__init__: there is no auth/session
        self.entries = entries
        self.page_size = page_size
        self.list_errors: Dict[str, int] = {}
        self.list_error_after_page: Dict[str, int] = {}
        self.get_errors: Dict[str, int] = {}
        self.get_error_reasons: Dict[str, str] = {}
        self.requests: List[Dict[str, Any]] = []

    def request(
        self,
        method: str,
        endpoint: str,
        params: Optional[ParamsType] = None,
        data: Optional[Union[str, bytes]] = None,
        json: Optional[Mapping[str, Any]] = None,
        files: Any = None,
        headers: Optional[MutableMapping[str, str]] = None,
        default_serializer: Any = None,
    ) -> Response:
        query = dict(params or {})
        self.requests.append({"method": method, "url": endpoint, "params": query})
        if endpoint == DRIVE_FILES_API_V3_URL:
            return as_response(self._list(query))
        prefix = DRIVE_FILES_API_V3_URL + "/"
        if endpoint.startswith(prefix):
            return as_response(self._get(endpoint[len(prefix) :], query))
        raise AssertionError("unexpected endpoint: {}".format(endpoint))

    def _error(
        self, code: int, message: str, reason: Optional[str] = None
    ) -> FakeResponse:
        details = {"reason": reason} if reason else {}
        return FakeResponse(
            {"error": {"code": code, "message": message, "errors": [details]}},
            code,
        )

    def _respond(self, response: FakeResponse) -> FakeResponse:
        if not response.ok:
            raise APIError(as_response(response))
        return response

    def _list(self, params: Dict[str, Any]) -> FakeResponse:
        query = params["q"]
        folder_id = query.split("'")[1]
        if folder_id in self.list_errors:
            response = self._error(self.list_errors[folder_id], "list failed")
            return self._respond(response)
        if folder_id in self.list_error_after_page:
            failed_from = self.list_error_after_page[folder_id]
            if int(str(params.get("pageToken") or "0")) >= failed_from:
                return self._respond(self._error(500, "page failed"))
        parent = self.entries[folder_id]
        children = [
            self.entries[child_id]
            for child_id in parent.get("children", [])
            if not self.entries[child_id].get("trashed")
        ]
        offset = int(str(params.get("pageToken") or "0"))
        page = children[offset : offset + self.page_size]
        nxt = offset + self.page_size
        body: Dict[str, Any] = {"files": page}
        if nxt < len(children):
            body["nextPageToken"] = str(nxt)
        return self._respond(FakeResponse(body))

    def _get(self, file_id: str, params: Dict[str, Any]) -> FakeResponse:
        if file_id in self.get_errors:
            code = self.get_errors[file_id]
            reason = self.get_error_reasons.get(file_id)
            return self._respond(self._error(code, "get failed", reason))
        entry = self.entries.get(file_id)
        if entry is None:
            return self._respond(self._error(404, "File not found"))
        return self._respond(FakeResponse(entry))


def file_entry(id: str, name: str, mime: str = SHEET, **extra: Any):
    entry = {"id": id, "name": name, "mimeType": mime}
    entry.update(extra)
    return entry


def shortcut_entry(id: str, name: str, target: str, target_mime: Optional[str]):
    details = {"targetId": target}
    if target_mime is not None:
        details["targetMimeType"] = target_mime
    return {
        "id": id,
        "name": name,
        "mimeType": SHORTCUT,
        "shortcutDetails": details,
    }


def folder_entry(
    id: str, name: str, children: Optional[List[str]] = None, **extra: Any
):
    entry = {"id": id, "name": name, "mimeType": FOLDER, "children": children or []}
    entry.update(extra)
    return entry


class DriveTestCase(unittest.TestCase):
    def make_client(self, drive: FakeDrive) -> Client:
        # Client.__init__ instantiates the http client; hand it the fake instead
        # so the traversal uses the in-memory Drive.
        factory = cast(HTTPClientType, lambda *a, **k: drive)
        client = Client(auth=None, http_client=factory)
        client.http_client = drive
        return client

    def collect(self, entries, folder: str = ENTRY, page_size: int = 1):
        drive = FakeDrive(entries, page_size=page_size)
        return self.make_client(drive).collect_spreadsheets(folder), drive

    def listed_folders(self, drive: FakeDrive) -> List[str]:
        """The distinct folder IDs listed, in request order."""
        seen: List[str] = []
        for request in drive.requests:
            if request["url"] != DRIVE_FILES_API_V3_URL:
                continue
            folder_id = request["params"]["q"].split("'")[1]
            if folder_id not in seen:
                seen.append(folder_id)
        return seen


class FolderWalkTest(DriveTestCase):
    def test_entry_folder_can_be_a_shortcut(self):
        entries = {
            "entry-link": shortcut_entry("entry-link", "entry link", ENTRY, FOLDER),
            ENTRY: folder_entry(ENTRY, "root", ["s"]),
            "s": file_entry("s", "sheet"),
        }
        result, _ = self.collect(entries, folder="entry-link")

        # paths are rooted at the shortcut target's real folder
        self.assertEqual(result[0].display_path, "root/sheet")

    def test_entry_folder_that_is_not_a_folder_raises(self):
        entries = {ENTRY: file_entry(ENTRY, "not a folder")}
        with self.assertRaises(InvalidInputValue):
            self.collect(entries)

    def test_trashed_entry_folder_raises(self):
        entries = {ENTRY: folder_entry(ENTRY, "root", ["s"], trashed=True)}
        with self.assertRaises(InvalidInputValue):
            self.collect(entries)

    def test_walks_nested_folders_and_paginates(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["a", "b", "s0"]),
            "a": folder_entry("a", "alpha", ["s1", "b"]),
            "b": folder_entry("b", "beta", ["s2"]),
            "s0": file_entry("s0", "root sheet"),
            "s1": file_entry("s1", "alpha sheet"),
            "s2": file_entry("s2", "beta sheet"),
        }
        result, drive = self.collect(entries, page_size=1)

        self.assertTrue(result.complete)
        self.assertEqual(
            [s.name for s in result], ["root sheet", "alpha sheet", "beta sheet"]
        )
        self.assertEqual(
            [s.display_path for s in result],
            ["root/root sheet", "root/alpha/alpha sheet", "root/beta/beta sheet"],
        )
        self.assertEqual(result.shortcut_failures, [])
        # every list call asked for the next page and only the first page was
        # returned per request
        list_requests = [
            r for r in drive.requests if r["url"] == DRIVE_FILES_API_V3_URL
        ]
        self.assertTrue(any("pageToken" in r["params"] for r in list_requests))

    def test_each_folder_is_listed_once(self):
        # "b" is reachable directly from the root and through "a"
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["a", "b"]),
            "a": folder_entry("a", "alpha", ["b"]),
            "b": folder_entry("b", "beta", ["s"]),
            "s": file_entry("s", "sheet"),
        }
        result, drive = self.collect(entries)

        listed = self.listed_folders(drive)
        self.assertEqual(listed.count(ENTRY), 1)
        self.assertEqual(listed.count("a"), 1)
        self.assertEqual(listed.count("b"), 1)
        # the shortest path wins: root/beta/sheet, not root/alpha/beta/sheet
        self.assertEqual(result[0].display_path, "root/beta/sheet")

    def test_shortcut_to_spreadsheet_keeps_shortcut_name(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["sc"]),
            "sc": shortcut_entry("sc", "shortcut name", "real", SHEET),
            "real": file_entry("real", "actual name"),
        }
        result, _ = self.collect(entries)

        self.assertEqual(len(result), 1)
        sheet = result[0]
        self.assertEqual(sheet.id, "real")
        self.assertEqual(sheet.name, "actual name")
        self.assertEqual(sheet.display_path, "root/shortcut name")
        self.assertEqual(sheet.path[-1].id, "real")
        self.assertEqual(sheet.mime_type, SHEET)

    def test_follows_shortcut_to_folder(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["sc"]),
            "sc": shortcut_entry("sc", "linked folder", "target-folder", FOLDER),
            "target-folder": folder_entry("target-folder", "target", ["s"]),
            "s": file_entry("s", "deep sheet"),
        }
        result, drive = self.collect(entries)

        self.assertEqual(result[0].display_path, "root/linked folder/deep sheet")
        listed = self.listed_folders(drive)
        self.assertIn("target-folder", listed)

    def test_ignores_stale_target_mime_hint(self):
        # hints say the opposite of the truth in both directions
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["to-folder", "to-sheet"]),
            "to-folder": shortcut_entry("to-folder", "f", "real-folder", SHEET),
            "real-folder": folder_entry("real-folder", "real", ["s"]),
            "to-sheet": shortcut_entry("to-sheet", "s", "real-sheet", FOLDER),
            "real-sheet": file_entry("real-sheet", "actual"),
            "s": file_entry("s", "nested"),
        }
        result, _ = self.collect(entries)

        self.assertEqual(
            sorted(s.display_path for s in result),
            ["root/f/nested", "root/s"],
        )

    def test_shortcut_chain(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["link1"]),
            "link1": shortcut_entry("link1", "first", "link2", SHEET),
            "link2": shortcut_entry("link2", "second", "real", SHEET),
            "real": file_entry("real", "actual"),
        }
        result, _ = self.collect(entries)

        self.assertEqual(len(result), 1)
        # the path keeps the name of the shortcut found in the folder
        self.assertEqual(result[0].display_path, "root/first")
        self.assertEqual(result[0].id, "real")

    def test_duplicate_spreadsheet_prefers_fewest_segments(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["a", "s"]),
            "a": folder_entry("a", "alpha", ["sc"]),
            "sc": shortcut_entry("sc", "via shortcut", "s", SHEET),
            "s": file_entry("s", "sheet"),
        }
        result, _ = self.collect(entries)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].display_path, "root/sheet")

    def test_tie_break_compares_names_then_ids(self):
        # Two equally long paths: root/zzz/sheet vs root/aaa/sheet
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["zzz", "aaa"]),
            "zzz": folder_entry("zzz", "zzz", ["sc-z"]),
            "aaa": folder_entry("aaa", "aaa", ["sc-a"]),
            "sc-z": shortcut_entry("sc-z", "b-name", "s", SHEET),
            "sc-a": shortcut_entry("sc-a", "a-name", "s", SHEET),
            "s": file_entry("s", "sheet"),
        }
        result, _ = self.collect(entries)
        self.assertEqual(len(result), 1)
        # segments after the root differ first at name: aaa < zzz
        self.assertEqual(result[0].display_path, "root/aaa/a-name")

    def test_tie_break_uses_id_when_names_are_equal(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["folder-b", "folder-a"]),
            "folder-a": folder_entry("folder-a", "same", ["sc-a"]),
            "folder-b": folder_entry("folder-b", "same", ["sc-b"]),
            "sc-a": shortcut_entry("sc-a", "leaf", "s", SHEET),
            "sc-b": shortcut_entry("sc-b", "leaf", "s", SHEET),
            "s": file_entry("s", "sheet"),
        }
        result, _ = self.collect(entries)
        self.assertEqual(result[0].display_path, "root/same/leaf")

    def test_shortcut_to_deleted_folder_is_reported_as_deleted(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["sc", "ok"]),
            "sc": shortcut_entry("sc", "dead folder", "gone-folder", FOLDER),
            "ok": file_entry("ok", "fine"),
        }
        result, _ = self.collect(entries)

        self.assertEqual(result.ids, ["ok"])
        failure = result.shortcut_failures[0]
        self.assertEqual(failure.reason, ShortcutFailureReason.deleted)
        self.assertEqual(failure.target_id, "gone-folder")
        self.assertTrue(result.complete)

    def test_shortcut_to_other_file_type_is_not_collected(self):
        # the hint says "spreadsheet" but the target is a PDF: the actual type
        # wins and the file is neither collected nor reported as a failure
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["sc"]),
            "sc": shortcut_entry("sc", "pdf link", "pdf", SHEET),
            "pdf": file_entry("pdf", "doc.pdf", mime=MimeType.pdf),
        }
        result, _ = self.collect(entries)

        self.assertEqual(result.ids, [])
        self.assertEqual(result.shortcut_failures, [])
        self.assertTrue(result.complete)

    def test_shortcut_without_target_is_an_error(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["broken", "ok"]),
            "broken": {
                "id": "broken",
                "name": "broken link",
                "mimeType": SHORTCUT,
                "shortcutDetails": {},
            },
            "ok": file_entry("ok", "fine"),
        }
        result, _ = self.collect(entries)

        self.assertEqual(result.ids, ["ok"])
        self.assertEqual(result.shortcut_failures, [])
        self.assertFalse(result.complete)
        self.assertEqual(result.errors[0].operation, "resolve_shortcut")
        self.assertEqual(result.errors[0].shortcut_id, "broken")

    def test_partial_listing_keeps_pages_read_before_the_failure(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["s1", "s2", "s3"]),
            "s1": file_entry("s1", "one"),
            "s2": file_entry("s2", "two"),
            "s3": file_entry("s3", "three"),
        }
        drive = FakeDrive(entries, page_size=1)
        drive.list_error_after_page[ENTRY] = 2
        result = self.make_client(drive).collect_spreadsheets(ENTRY)

        self.assertFalse(result.complete)
        self.assertEqual(sorted(result.ids), ["s1", "s2"])
        self.assertEqual(result.incomplete_directories[0].id, ENTRY)

    def test_parent_cycle_terminates(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["a"]),
            "a": folder_entry("a", "alpha", ["back", "s"]),
            "back": shortcut_entry("back", "up", ENTRY, FOLDER),
            "s": file_entry("s", "sheet"),
        }
        result, drive = self.collect(entries)

        self.assertEqual(self.listed_folders(drive).count(ENTRY), 1)
        self.assertEqual(result[0].display_path, "root/alpha/sheet")

    def test_self_referencing_shortcut_terminates(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["self"]),
            "self": shortcut_entry("self", "loop", "loop-target", FOLDER),
            "loop-target": folder_entry("loop-target", "loop", ["self2"]),
            "self2": shortcut_entry("self2", "again", "loop-target", FOLDER),
        }
        result, _ = self.collect(entries)
        self.assertEqual(len(result), 0)
        self.assertEqual(result.errors, [])

    def test_accepts_folder_url(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["s"]),
            "s": file_entry("s", "sheet"),
        }
        result, _ = self.collect(
            entries,
            folder="https://drive.google.com/drive/folders/entry?usp=sharing",
        )
        self.assertEqual(result.ids, ["s"])

    def test_other_file_types_are_skipped(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["doc", "s"]),
            "doc": file_entry("doc", "a doc", mime=MimeType.pdf),
            "s": file_entry("s", "sheet"),
        }
        result, _ = self.collect(entries)
        self.assertEqual(result.ids, ["s"])


class ShortcutFailureTest(DriveTestCase):

    def test_deleted_target_is_reported_not_collected(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["gone"]),
            "gone": shortcut_entry("gone", "dead link", "missing", SHEET),
        }
        result, _ = self.collect(entries)

        self.assertEqual(result.ids, [])
        self.assertEqual(len(result.shortcut_failures), 1)
        failure = result.shortcut_failures[0]
        self.assertEqual(failure.reason, ShortcutFailureReason.deleted)
        self.assertEqual(failure.shortcut_id, "gone")
        self.assertEqual(failure.shortcut_name, "dead link")
        self.assertEqual(failure.target_id, "missing")
        self.assertEqual(failure.path[-1].name, "dead link")
        # expected findings do not make the traversal incomplete
        self.assertTrue(result.complete)

    def test_trashed_target_is_reported_as_deleted(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["gone"]),
            "gone": shortcut_entry("gone", "trashed", "trashed-id", SHEET),
            "trashed-id": file_entry("trashed-id", "x", trashed=True),
        }
        result, _ = self.collect(entries)

        self.assertEqual(result.ids, [])
        self.assertEqual(
            result.shortcut_failures[0].reason, ShortcutFailureReason.deleted
        )

    def test_forbidden_target_is_reported_separately(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["secret"]),
            "secret": shortcut_entry("secret", "no access", "private", SHEET),
            "private": file_entry("private", "x"),
        }
        drive = FakeDrive(entries)
        drive.get_errors["private"] = 403
        client = self.make_client(drive)
        result = client.collect_spreadsheets(ENTRY)

        self.assertEqual(result.ids, [])
        self.assertEqual(
            result.shortcut_failures[0].reason,
            ShortcutFailureReason.permission_denied,
        )
        self.assertTrue(result.complete)

    def test_404_with_permission_reason_is_not_reported_as_deleted(self):
        # Drive answers 404 for a file shared with someone else as well
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["secret"]),
            "secret": shortcut_entry("secret", "no access", "private", SHEET),
            "private": file_entry("private", "x"),
        }
        drive = FakeDrive(entries)
        drive.get_errors["private"] = 404
        drive.get_error_reasons["private"] = "insufficientFilePermissions"
        result = self.make_client(drive).collect_spreadsheets(ENTRY)

        self.assertEqual(result.ids, [])
        self.assertEqual(
            result.shortcut_failures[0].reason,
            ShortcutFailureReason.permission_denied,
        )

    def test_404_with_not_found_reason_is_deleted(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["gone"]),
            "gone": shortcut_entry("gone", "dead", "missing", SHEET),
        }
        drive = FakeDrive(entries)
        drive.get_errors["missing"] = 404
        drive.get_error_reasons["missing"] = "fileNotFound"
        result = self.make_client(drive).collect_spreadsheets(ENTRY)

        self.assertEqual(
            result.shortcut_failures[0].reason, ShortcutFailureReason.deleted
        )

    def test_reasons_are_reported_per_shortcut(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["dead", "private"]),
            "dead": shortcut_entry("dead", "dead", "missing", SHEET),
            "private": shortcut_entry("private", "private", "secret", SHEET),
            "secret": file_entry("secret", "x"),
        }
        drive = FakeDrive(entries)
        drive.get_errors["secret"] = 403
        client = self.make_client(drive)
        result = client.collect_spreadsheets(ENTRY)

        reasons = {f.shortcut_name: f.reason for f in result.shortcut_failures}
        self.assertEqual(
            reasons,
            {
                "dead": ShortcutFailureReason.deleted,
                "private": ShortcutFailureReason.permission_denied,
            },
        )


class CompletenessTest(DriveTestCase):

    def test_folder_listing_error_marks_traversal_incomplete(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["ok", "broken"]),
            "ok": file_entry("ok", "fine"),
            "broken": folder_entry("broken", "broken", ["s"]),
            "s": file_entry("s", "hidden"),
        }
        drive = FakeDrive(entries)
        drive.list_errors["broken"] = 500
        client = self.make_client(drive)
        result = client.collect_spreadsheets(ENTRY)

        self.assertFalse(result.complete)
        self.assertEqual(result.ids, ["ok"])
        self.assertEqual(len(result.errors), 1)
        error = result.errors[0]
        self.assertEqual(error.operation, "list_folder")
        self.assertEqual(error.folder_id, "broken")
        self.assertEqual(error.status_code, 500)
        self.assertEqual([d.id for d in result.incomplete_directories], ["broken"])
        self.assertTrue(result.directories[0].complete)

    def test_shortcut_lookup_error_is_recorded_but_not_a_shortcut_failure(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["sc", "ok"]),
            "sc": shortcut_entry("sc", "link", "target", SHEET),
            "target": file_entry("target", "x"),
            "ok": file_entry("ok", "fine"),
        }
        drive = FakeDrive(entries)
        drive.get_errors["target"] = 500
        client = self.make_client(drive)
        result = client.collect_spreadsheets(ENTRY)

        self.assertFalse(result.complete)
        self.assertEqual(result.ids, ["ok"])
        self.assertEqual(result.shortcut_failures, [])
        self.assertEqual(result.errors[0].operation, "resolve_shortcut")
        self.assertEqual(result.errors[0].shortcut_id, "sc")

    def test_unreadable_entry_folder_raises(self):
        entries = {ENTRY: folder_entry(ENTRY, "root", [])}
        drive = FakeDrive(entries)
        drive.get_errors[ENTRY] = 404
        client = self.make_client(drive)
        with self.assertRaises(APIError):
            client.collect_spreadsheets(ENTRY)

    def test_request_fields_include_shortcut_details_and_actual_type(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["sc"]),
            "sc": shortcut_entry("sc", "link", "s", SHEET),
            "s": file_entry("s", "sheet"),
        }
        _, drive = self.collect(entries)
        list_params = [
            r["params"] for r in drive.requests if r["url"] == DRIVE_FILES_API_V3_URL
        ]
        self.assertIn("shortcutDetails", list_params[0]["fields"])
        tests = [
            r["params"]
            for r in drive.requests
            if r["url"].startswith(DRIVE_FILES_API_V3_URL + "/")
        ]
        self.assertTrue(all("mimeType" in p["fields"] for p in tests))


class PublicApiTest(DriveTestCase):
    def test_extract_folder_id(self):
        self.assertEqual(extract_folder_id("abc123"), "abc123")
        self.assertEqual(
            extract_folder_id("https://drive.google.com/drive/u/0/folders/xyz"),
            "xyz",
        )
        self.assertEqual(
            extract_folder_id("https://drive.google.com/open?id=abc-123_X"),
            "abc-123_X",
        )

    def test_extract_folder_id_rejects_non_folder_input(self):
        with self.assertRaises(InvalidInputValue):
            extract_folder_id("https://example.com/not-a-folder")
        with self.assertRaises(InvalidInputValue):
            extract_folder_id("not a folder id")

    def test_exports(self):
        for name in [
            "SpreadsheetCollection",
            "CollectedSpreadsheet",
            "ShortcutFailure",
            "ShortcutFailureReason",
            "CollectionError",
            "DirectoryRecord",
            "PathSegment",
        ]:
            self.assertIn(name, gspread.__all__)
            self.assertTrue(hasattr(gspread, name))

    def test_collected_spreadsheet_url(self):
        entries = {
            ENTRY: folder_entry(ENTRY, "root", ["s"]),
            "s": file_entry("s", "sheet"),
        }
        result, _ = self.collect(entries)
        self.assertEqual(
            result[0].url,
            "https://docs.google.com/spreadsheets/d/s",
        )


if __name__ == "__main__":
    unittest.main()
