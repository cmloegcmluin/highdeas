"""File a memo's transcript into Google Drive as a real, native Google Doc --
created through the actual Drive API, not the docx-into-a-locally-mirrored-folder
trick routers.write_docx still falls back to.

This authenticates as Douglas's own Google account (OAuth "user" credentials),
never as the service account drive_link.py reads with. That distinction isn't a
style choice: a service account has no Drive storage quota of its own, and Google
enforces this hard -- it cannot own a newly created file inside a personal
(non-Workspace) "My Drive" at all, confirmed against Google's own error message
for it ("Service Accounts do not have storage quota"), not assumed. Only a real
signed-in user can own a new file, so creating one has to happen as Douglas,
via a one-time browser consent (scripts/authorize_google_docs.py) that leaves a
refresh token behind for every run after.

That token is scoped to drive.file, deliberately the narrowest Drive scope
Google offers: full drive access exists too, but it is a "restricted" scope
that requires an annual third-party security assessment (Google's CASA program)
to use outside of Google's own review -- a paid audit process built for
companies, not a one-person tool. drive.file avoids it, at a real cost this
module is built around: a client holding only drive.file can never see or
write into a folder it did not itself create (confirmed against Google's own
Drive API docs and reports of the exact "insufficient permissions" failure this
would otherwise hit) -- not one the Drive website made, not one Drive for
Desktop synced up from a local folder, even shared Editor. So the folder tree
this files into (HIGHDEAS_DRIVE_DOCS_FOLDER_NAME, dated subfolders beneath it)
is entirely its own, separate from HIGHDEAS_DRIVE_BASE, the folder the audio
copy still goes to (routers.DriveMusicRouter) -- nesting into that one instead
would need either the broader restricted scope (the CASA assessment above) or
Douglas re-granting access through Drive's file picker for every subfolder ever
created, a worse one-time setup than a second folder tree in exchange for."""
import json

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from highdeas.drive_link import _escaped

# The narrowest Drive scope that can create files at all: see the module
# docstring for why this, and not the full (and restricted) drive scope.
TOKEN_SCOPE = "https://www.googleapis.com/auth/drive.file"

_FILES_ENDPOINT = "https://www.googleapis.com/drive/v3/files"
_UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/drive/v3/files"


def _user_access_token(token_file, *, credentials_cls=Credentials):
    """A fresh OAuth access token for the Google account authorized into
    `token_file` (the file scripts/authorize_google_docs.py writes after Douglas
    signs in once) -- or "" without one configured. The caller that builds a
    DriveDocFiler is already None in that case (see app._drive_doc_filer), but
    this stays defensive rather than assume it's never reached any other way,
    the same posture drive_link._service_account_token takes."""
    if not token_file:
        return ""
    credentials = credentials_cls.from_authorized_user_file(token_file, scopes=[TOKEN_SCOPE])
    credentials.refresh(Request())
    return credentials.token


_DOC_MIME_TYPE = "application/vnd.google-apps.document"
_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
# Arbitrary but fixed: it only has to not appear inside the parts it separates,
# and neither JSON metadata nor a memo's own HTML ever will.
_BOUNDARY = "----highdeas-drive-write-boundary"


def _multipart_related_body(parts):
    """The multipart/related body (RFC 2387) Drive's own "multipart upload"
    protocol expects for files.create -- one part per (content, content_type)
    pair, metadata first -- and the Content-Type header (boundary included) to
    send alongside it. Built by hand rather than left to requests' own
    multipart support: that builds multipart/form-data (an HTML file input's
    format), a different wire shape than the multipart/related Drive's own
    examples show, and this way what's sent is exactly that, byte for byte."""
    pieces = [f"--{_BOUNDARY}\r\nContent-Type: {content_type}\r\n\r\n{content}"
              for content, content_type in parts]
    body = "\r\n".join(pieces) + f"\r\n--{_BOUNDARY}--"
    return body, f"multipart/related; boundary={_BOUNDARY}"


class DriveDocFiler:
    """Files a memo's transcript into Google Drive as a real, native Google Doc,
    inside `container_name`/<dated subfolder> -- both created the first time
    they're needed and reused after (see the module docstring for why this
    can only ever be a folder tree of its own, never HIGHDEAS_DRIVE_BASE)."""

    def __init__(self, token_file, container_name, *, get=requests.get, post=requests.post,
                 token=_user_access_token):
        self._token_file = token_file
        self._container_name = container_name
        self._get = get
        self._post = post
        self._token = token

    def _folder_id(self, headers, name, parent_id):
        """The id of the folder named `name` directly inside `parent_id`
        ("root" for Drive's own top level) -- the one already there, or one
        just created, so every caller gets back a folder that exists either
        way."""
        query = (
            f"name = '{_escaped(name)}' and "
            f"mimeType = '{_FOLDER_MIME_TYPE}' and "
            f"'{parent_id or 'root'}' in parents and "
            "trashed = false"
        )
        response = self._get(_FILES_ENDPOINT, headers=headers,
                             params={"q": query, "fields": "files(id)"}, timeout=10)
        response.raise_for_status()
        files = response.json().get("files", [])
        if files:
            return files[0]["id"]
        metadata = {"name": name, "mimeType": _FOLDER_MIME_TYPE}
        if parent_id:
            metadata["parents"] = [parent_id]
        response = self._post(_FILES_ENDPOINT, headers=headers, json=metadata,
                              params={"fields": "id"}, timeout=10)
        response.raise_for_status()
        return response.json()["id"]

    def file_doc(self, subfolder_name, title, html):
        """Create `title` as a native Google Doc holding `html`, inside
        container_name/subfolder_name. Returns the doc's own Drive link, or ""
        when it can't be filed at all: not configured, the token can't be
        obtained, or any call along the way fails -- the same fall-back-quiet
        contract as DriveFolderLinker.link_for, so a Drive hiccup degrades to
        the docx-in-a-local-folder fallback (routers.DriveMusicRouter) rather
        than losing the memo's routing."""
        if not self._token_file or not subfolder_name or not title:
            return ""
        try:
            access_token = self._token(self._token_file)
        except Exception:  # noqa: BLE001 — a missing/invalid/revoked token must fall back quietly
            return ""
        if not access_token:
            return ""
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            container_id = self._folder_id(headers, self._container_name, "")
            subfolder_id = self._folder_id(headers, subfolder_name, container_id)
            metadata = {"name": title, "mimeType": _DOC_MIME_TYPE, "parents": [subfolder_id]}
            body, content_type = _multipart_related_body(
                ((json.dumps(metadata), "application/json"), (html, "text/html; charset=UTF-8")))
            response = self._post(
                _UPLOAD_ENDPOINT,
                headers={**headers, "Content-Type": content_type},
                params={"uploadType": "multipart", "fields": "id"},
                data=body,
                timeout=30,
            )
            response.raise_for_status()
            doc_id = response.json()["id"]
        except Exception:  # noqa: BLE001 — offline/misconfigured/revoked must fall back quietly, not 500
            return ""
        return f"https://docs.google.com/document/d/{doc_id}/edit"
