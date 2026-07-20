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
companies, not a one-person tool.

drive.file's real cost is narrower than it first looks, though -- confirmed
live, against Douglas's own real Drive, on 2026-07-19, not assumed from
Google's docs alone (an earlier version of this module got that wrong, from
having no live token to actually try it against yet). A drive.file-scoped
client genuinely cannot *discover* a folder it did not itself create: a
files.get on a foreign folder's own id 404s ("File not found"), and a
files.list search for it by name comes back empty, even though the same
folder is sitting right there in Douglas's own My Drive, fully owned by the
very account that granted this token. But if that folder's id is already
known -- learned some other way, e.g. drive_link.py's own service-account
lookup -- a drive.file-scoped client *can* write into it: both creating a new
file there directly (parents: [that id]), and moving a file this app already
created into it afterward (files.update?addParents=that
id&removeParents=<wherever it was>). Both calls came back 200, and the second
was independently confirmed by asking the read-only service account (a
completely different credential) to list that folder's own contents and
finding the moved doc sitting there. Apparently the write path checks the
authenticated *account's* Drive permissions on the destination, where the
read path enforces the app's own narrower visibility -- undocumented as far
as this project found, but repeatable.

So DriveDocFiler still files into its own container first
(HIGHDEAS_DRIVE_DOCS_FOLDER_NAME, dated subfolders beneath it, entirely
separate from HIGHDEAS_DRIVE_BASE) -- that part hasn't changed, because it's
still the one destination guaranteed resolvable at the moment a memo is
filed, the same instant its own dated HIGHDEAS_DRIVE_BASE subfolder may not
even exist in Drive's cloud yet (Drive for Desktop uploads a locally-created
folder on its own schedule -- see drive_link.py). But once filed there, given
a way to resolve the audio's own dated folder's id (find_folder_id below --
app.py wires up drive_link.DriveFolderLinker.id_for, the same service account
already used for the bin's Drive icon), file_doc now makes that confirmed-
working addParents/removeParents call as a best-effort last step, landing the
doc beside its audio after all.

The container is only ever a *temporary* holding pen now, not a permanent
fallback: when that first attempt's resolution comes back empty -- chiefly
the very first music memo of a new day, filed before that day's brand new
subfolder has synced up to Drive's cloud yet -- file_doc reports the doc as
still needing a move (store.Memo.drive_doc_needs_move), and
DriveDocReconciler, on its own clock (app._reconcile_drive_docs_continuously),
keeps retrying reconcile() for it until that folder exists and the move
finally lands. So every doc still ends up beside its audio eventually, not
just every one after a day's first.

Landing beside its audio -- whether on file_doc's own first try or only
later, once DriveDocReconciler catches up to it -- leaves its container
subfolder empty. DriveDocReconciler sweeps every subfolder it finds settled
this way with remove_if_empty, not just the ones it just reconciled, so the
container doesn't accumulate one empty dated subfolder per day forever;
file_doc's own synchronous path never attempts this itself, since the
common case (the folder was already resolvable) would otherwise cost every
single filing an extra Drive round trip just to clean up a subfolder the
very next memo that day is likely to reuse anyway."""
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


_DOC_LINK_PREFIX = "https://docs.google.com/document/d/"


def _doc_id_from_link(link):
    """The doc id out of a link file_doc itself once handed back
    (f"{_DOC_LINK_PREFIX}{doc_id}/edit"), or "" from anything else -- blank,
    or some other page's link entirely. reconcile has nothing to retry
    without one, and must not be tricked into treating a stray "/" in some
    foreign URL as a real id."""
    if not (link or "").startswith(_DOC_LINK_PREFIX):
        return ""
    doc_id, sep, _rest = link[len(_DOC_LINK_PREFIX):].partition("/")
    return doc_id if sep else ""


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
    they're needed and reused after. When `find_folder_id` can resolve the
    audio's own dated HIGHDEAS_DRIVE_BASE subfolder to its Drive id, the doc is
    then moved beside the audio itself as a last, best-effort step (see the
    module docstring for why filing always starts in the container regardless,
    and for how that move is possible at all under drive.file scope)."""

    def __init__(self, token_file, container_name, *, get=requests.get, post=requests.post,
                 patch=requests.patch, token=_user_access_token, find_folder_id=None):
        self._token_file = token_file
        self._container_name = container_name
        self._get = get
        self._post = post
        self._patch = patch
        self._token = token
        self._find_folder_id = find_folder_id

    def _find_folder(self, headers, name, parent_id):
        """The id of the folder named `name` directly inside `parent_id`
        ("root" for Drive's own top level), or "" when there isn't one --
        the search half of _folder_id, split out because remove_if_empty
        must only ever look for a container/subfolder, never create one on
        its way to checking whether it's empty."""
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
        return files[0]["id"] if files else ""

    def _folder_id(self, headers, name, parent_id):
        """The id of the folder named `name` directly inside `parent_id`
        ("root" for Drive's own top level) -- the one already there, or one
        just created, so every caller gets back a folder that exists either
        way."""
        found = self._find_folder(headers, name, parent_id)
        if found:
            return found
        metadata = {"name": name, "mimeType": _FOLDER_MIME_TYPE}
        if parent_id:
            metadata["parents"] = [parent_id]
        response = self._post(_FILES_ENDPOINT, headers=headers, json=metadata,
                              params={"fields": "id"}, timeout=10)
        response.raise_for_status()
        return response.json()["id"]

    def file_doc(self, subfolder_name, title, html):
        """Create `title` as a native Google Doc holding `html`, inside
        container_name/subfolder_name -- then, when find_folder_id was given,
        try once to move it beside the audio itself (see _move_beside_the_audio).
        Returns (link, needs_move): link is the doc's own Drive link, or ""
        when it can't be filed at all -- not configured, the token can't be
        obtained, or any call along the way fails -- the same fall-back-quiet
        contract as DriveFolderLinker.link_for, so a Drive hiccup degrades to
        the docx-in-a-local-folder fallback (routers.DriveMusicRouter) rather
        than losing the memo's routing. Once a doc exists, though, that link
        is returned regardless of whether the move afterward found anywhere
        to go or worked -- the doc it names is real either way, just not
        always beside its audio yet. needs_move says which: True when a doc
        exists but is still stranded in the container and should be retried
        later (DriveDocReconciler); always False without one (nothing was
        ever attempted, so nothing to retry)."""
        if not self._token_file or not subfolder_name or not title:
            return "", False
        try:
            access_token = self._token(self._token_file)
        except Exception:  # noqa: BLE001 — a missing/invalid/revoked token must fall back quietly
            return "", False
        if not access_token:
            return "", False
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
            return "", False
        needs_move = False
        if self._find_folder_id is not None:
            needs_move = self._move_beside_the_audio(headers, doc_id, subfolder_id, subfolder_name)
        return f"https://docs.google.com/document/d/{doc_id}/edit", needs_move

    def _move_beside_the_audio(self, headers, doc_id, current_parent_id, subfolder_name):
        """Best-effort: relocate the doc file_doc just filed in its own
        container into the audio's own dated HIGHDEAS_DRIVE_BASE folder
        instead, via _move_to -- the files.update addParents/removeParents
        call confirmed (see the module docstring) to work under drive.file
        scope for a foreign folder whose id is already known -- which is
        exactly what find_folder_id(subfolder_name) is for.

        Returns whether the doc still needs a move: False when it's already
        exactly where it belongs (moved just now, or found already there),
        True when it's stranded and should be retried later
        (DriveDocReconciler) -- a resolution miss (that folder hasn't synced
        up to Drive's cloud yet), a resolver that raised, or an outright
        failed move are all routine, not exceptional -- the doc that already
        exists, and the link file_doc already returns for it, must never be
        put in doubt by what happens in here."""
        try:
            target_id = self._find_folder_id(subfolder_name)
        except Exception:  # noqa: BLE001 — a flaky resolver just means: try again later
            return True
        if not target_id:
            return True
        if target_id == current_parent_id:
            return False
        return not self._move_to(headers, doc_id, target_id, current_parent_id)

    def _move_to(self, headers, doc_id, target_id, current_parent_id):
        """The files.update addParents/removeParents call itself -- shared by
        file_doc's own first attempt (_move_beside_the_audio) and reconcile's
        later retry, the only two places that ever need to actually move a
        doc. Returns whether the move landed; swallows everything else so a
        failed move never raises past either best-effort caller."""
        try:
            response = self._patch(
                f"{_FILES_ENDPOINT}/{doc_id}",
                headers=headers,
                params={"addParents": target_id, "removeParents": current_parent_id, "fields": "id"},
                timeout=10,
            )
            response.raise_for_status()
            return True
        except Exception:  # noqa: BLE001 — best-effort; the doc already filed must stand regardless
            return False

    def reconcile(self, link, subfolder_name):
        """Retry, later, the move file_doc's own best-effort attempt left
        stranded: called by DriveDocReconciler for every retired memo still
        flagged Memo.drive_doc_needs_move. `link` is the doc's own link, as
        file_doc returned it -- reconcile recovers the doc id from it (see
        _doc_id_from_link) rather than taking one separately, so a caller
        only ever has to carry the one value store.Memo already keeps.

        Returns whether the doc can now be considered settled beside its
        audio: True when the move lands here, or turns out to have already
        landed (a previous pass's move that succeeded but whose flag never
        got cleared -- a crash in between, say); False when it's still not
        resolvable or the attempt fails, so the caller knows to leave the
        flag set for the next pass."""
        if self._find_folder_id is None:
            return False
        doc_id = _doc_id_from_link(link)
        if not doc_id:
            return False
        try:
            target_id = self._find_folder_id(subfolder_name)
        except Exception:  # noqa: BLE001 — a flaky resolver just means: try again next pass
            return False
        if not target_id:
            return False
        try:
            access_token = self._token(self._token_file)
            if not access_token:
                return False
            headers = {"Authorization": f"Bearer {access_token}"}
            response = self._get(f"{_FILES_ENDPOINT}/{doc_id}", headers=headers,
                                 params={"fields": "parents"}, timeout=10)
            response.raise_for_status()
            parents = response.json().get("parents", [])
        except Exception:  # noqa: BLE001 — offline/revoked/deleted must fall back quietly, not 500
            return False
        if target_id in parents:
            return True
        current_parent_id = parents[0] if parents else ""
        return self._move_to(headers, doc_id, target_id, current_parent_id)

    def remove_if_empty(self, subfolder_name):
        """Trash container_name/subfolder_name once every doc that had been
        filed there has moved on -- called after a DriveDocReconciler pass
        settles every memo sharing it, so the container doesn't accumulate
        one empty dated subfolder per day forever (see the module
        docstring). Searches only, via _find_folder -- unlike _folder_id,
        never creates: a subfolder that isn't there yet is not this method's
        business to make, and neither is the container itself. Swallows
        everything, the same fall-back-quiet contract as the rest of this
        class -- a cleanup that fails just leaves a harmless empty folder
        behind, not a broken reconciliation pass."""
        if not self._token_file or not subfolder_name:
            return
        try:
            access_token = self._token(self._token_file)
            if not access_token:
                return
            headers = {"Authorization": f"Bearer {access_token}"}
            container_id = self._find_folder(headers, self._container_name, "")
            if not container_id:
                return
            subfolder_id = self._find_folder(headers, subfolder_name, container_id)
            if not subfolder_id:
                return
            query = f"'{subfolder_id}' in parents and trashed = false"
            response = self._get(_FILES_ENDPOINT, headers=headers,
                                 params={"q": query, "fields": "files(id)"}, timeout=10)
            response.raise_for_status()
            if response.json().get("files", []):
                return
            response = self._patch(f"{_FILES_ENDPOINT}/{subfolder_id}", headers=headers,
                                   json={"trashed": True}, timeout=10)
            response.raise_for_status()
        except Exception:  # noqa: BLE001 — cleanup is best-effort; must never break reconciliation
            pass


class DriveDocReconciler:
    """The periodic pass that finishes what file_doc's own best-effort move
    could only start (see the module docstring): retries reconcile() for
    every retired memo still flagged Memo.drive_doc_needs_move, until each
    one's doc is beside its audio -- not just every memo after a day's
    first, the gap file_doc alone leaves. Once a memo's doc is settled
    beside its audio -- whether that just happened here, or it moved on
    file_doc's own first try and was never flagged at all -- this also
    sweeps its container subfolder with remove_if_empty, so the one-empty-
    dated-subfolder-per-day leftover (file_doc's own synchronous path never
    has time to clean up after itself, and most days never strand a single
    doc for this to catch otherwise) doesn't linger forever either.
    app.py calls run_once on its own clock (_reconcile_drive_docs_continuously)."""

    def __init__(self, store, filer):
        self._store = store
        self._filer = filer

    def run_once(self):
        """One pass over every retired memo that ever filed a doc to the
        container: retry the move for any still flagged as needing one,
        clearing the flag for each that lands -- then sweep every subfolder
        that's now settled (freshly reconciled here, or never flagged to
        begin with) with remove_if_empty, once each, after every memo
        sharing it has had its turn. A memo with nothing ever filed (a
        different route, a blank transcript, Drive Doc filing not
        configured at submit time) is skipped outright; one still stranded
        (not yet resolvable, or reconcile fails again) is left flagged and
        its subfolder is never swept -- the doc is still sitting in it."""
        subfolders_settled = []
        for memo in self._store.list_retired():
            if not memo.drive_doc_link or not memo.drive_subfolder:
                continue
            if memo.drive_doc_needs_move:
                try:
                    moved = self._filer.reconcile(memo.drive_doc_link, memo.drive_subfolder)
                except Exception:  # noqa: BLE001 — one bad memo must never stop the rest
                    continue
                if not moved:
                    continue
                self._store.update(memo.audio_filename, drive_doc_needs_move=False)
            if memo.drive_subfolder not in subfolders_settled:
                subfolders_settled.append(memo.drive_subfolder)
        for subfolder_name in subfolders_settled:
            self._filer.remove_if_empty(subfolder_name)
