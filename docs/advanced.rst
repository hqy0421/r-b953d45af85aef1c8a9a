Advanced Usage
==============

Custom Authentication
---------------------

Google Colaboratory
~~~~~~~~~~~~~~~~~~~

If you familiar with the Jupyter Notebook, `Google Colaboratory <https://colab.research.google.com/>`_ is probably the easiest way to get started using gspread::

    from google.colab import auth
    auth.authenticate_user()

    import gspread
    from google.auth import default
    creds, _ = default()

    gc = gspread.authorize(creds)

See the full example in the `External data: Local Files, Drive, Sheets, and Cloud Storage <https://colab.research.google.com/notebooks/io.ipynb#scrollTo=sOm9PFrT8mGG>`_ notebook.


Using Authlib
~~~~~~~~~~~~~

Using ``Authlib`` instead of ``google-auth``. Similar to `google.auth.transport.requests.AuthorizedSession <https://google-auth.readthedocs.io/en/latest/reference/google.auth.transport.requests.html#google.auth.transport.requests.AuthorizedSession>`_ Authlib's ``AssertionSession`` can automatically refresh tokens.::

    import json
    from gspread import Client
    from authlib.integrations.requests_client import AssertionSession

    def create_assertion_session(conf_file, scopes, subject=None):
        with open(conf_file, 'r') as f:
            conf = json.load(f)

        token_url = conf['token_uri']
        issuer = conf['client_email']
        key = conf['private_key']
        key_id = conf.get('private_key_id')

        header = {'alg': 'RS256'}
        if key_id:
            header['kid'] = key_id

        # Google puts scope in payload
        claims = {'scope': ' '.join(scopes)}
        return AssertionSession(
            grant_type=AssertionSession.JWT_BEARER_GRANT_TYPE,
            token_endpoint=token_url,
            issuer=issuer,
            audience=token_url,
            claims=claims,
            subject=subject,
            key=key,
            header=header,
        )

    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ]
    session = create_assertion_session('your-google-conf.json', scopes)
    gc = Client(None, session)

    wks = gc.open("Where is the money Lebowski?").sheet1

    wks.update_acell('B2', "it's down there somewhere, let me take another look.")

    # Fetch a cell range
    cell_list = wks.range('A1:B7')


Collecting Spreadsheets From a Folder
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:meth:`~gspread.Client.collect_spreadsheets` walks a Drive folder tree and
returns every Google Sheet the authenticated user can access:

.. code:: python

   result = gc.collect_spreadsheets("1AbCdEfGhIjKlMnOpQrStUvWxYz")

   for sheet in result:
       print(sheet.id, sheet.display_path, sheet.url)

Sub-folders are listed page by page, and Drive shortcuts are followed whether
they point at a folder or at a spreadsheet.  A folder can also be given as its
Drive URL, e.g. ``https://drive.google.com/drive/folders/<folder_id>``.

Results are deduplicated by the target's real ID.  When a spreadsheet is
reachable through several paths, the one with the fewest directory segments is
kept; ties are broken by comparing each path segment's name then ID, in order.
A path segment created by a shortcut keeps the shortcut's name, while its ID is
the real target ID.  A folder reached through several entries is listed only
once, which also makes cycles such as a shortcut back to a parent folder safe.

Shortcut targets that are deleted, trashed, or not viewable are never reported
as collected.  They appear separately, with a reason:

.. code:: python

   for failure in result.shortcut_failures:
       print(failure.shortcut_name, failure.reason)
       # deleted  /  permission_denied

The actual MIME type of a shortcut target is always fetched; the
``targetMimeType`` hint returned by the Drive API is ignored because it can be
stale.

If anything else goes wrong, the corresponding request error is recorded and
``result.complete`` is ``False``, so you can tell that this tree's listing may
be missing files:

.. code:: python

   if not result.complete:
       for error in result.errors:
           print(error.operation, error.path[-1].name, error.message)
