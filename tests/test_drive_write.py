"""Tests for filing a memo's transcript into Google Drive as a real, native
Google Doc -- via the actual Drive API, authenticated as Douglas's own Google
account rather than a service account (see drive_write.py's module docstring
for why a service account can't own a file in a personal Drive)."""
from highdeas.drive_write import (
    TOKEN_SCOPE, DriveDocFiler, DriveDocReconciler, _user_access_token,
)


def test_user_access_token_reads_the_token_file_and_returns_the_access_token():
    calls = []

    class FakeCredentials:
        token = "fake-user-access-token"

        def refresh(self, request):
            calls.append("refreshed")

        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            calls.append((path, scopes))
            return cls()

    token = _user_access_token("token.json", credentials_cls=FakeCredentials)

    assert token == "fake-user-access-token"
    assert calls == [("token.json", [TOKEN_SCOPE]), "refreshed"]


def test_user_access_token_is_blank_without_a_token_file():
    assert _user_access_token("") == ""


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class FakeCalls:
    """Answers each call with the next queued response, in order -- file_doc
    makes a fixed sequence of them (search container, maybe create it, search
    subfolder, maybe create it, create the doc), each needing its own canned
    reply, so a single fixed body per fake (as drive_link's FakeGet uses) isn't
    enough here."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


def _no_files():
    return FakeResponse(body={"files": []})


def _found(file_id):
    return FakeResponse(body={"files": [{"id": file_id}]})


def _created(file_id):
    return FakeResponse(body={"id": file_id})


def test_file_doc_blank_without_a_token_file():
    filer = DriveDocFiler("", "Highdeas Voice Memo Docs")
    assert filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Title", "<p>hi</p>") == ("", False)


def test_file_doc_blank_without_a_subfolder_name_or_title():
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", token=lambda f: "tok")
    assert filer.file_doc("", "Title", "<p>hi</p>") == ("", False)
    assert filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "", "<p>hi</p>") == ("", False)


def test_file_doc_blank_when_the_token_cant_be_obtained():
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", token=lambda f: "")
    assert filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Title", "<p>hi</p>") == ("", False)


def test_file_doc_creates_the_container_and_subfolder_when_neither_exists_yet():
    get = FakeCalls(_no_files(), _no_files())
    post = FakeCalls(_created("CONTAINER_ID"), _created("SUBFOLDER_ID"), _created("DOC_ID"))
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs",
                          get=get, post=post, token=lambda f: "tok-123")

    link, needs_move = filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Korok Dance",
                                      "<p>la la la</p>")

    assert link == "https://docs.google.com/document/d/DOC_ID/edit"
    assert needs_move is False  # no resolver configured -- nothing to ever retry
    # Container searched for at root, then created there.
    search_url, search_kwargs = get.calls[0]
    assert search_url == "https://www.googleapis.com/drive/v3/files"
    assert "name = 'Highdeas Voice Memo Docs'" in search_kwargs["params"]["q"]
    assert "'root' in parents" in search_kwargs["params"]["q"]
    create_url, create_kwargs = post.calls[0]
    assert create_url == "https://www.googleapis.com/drive/v3/files"
    assert create_kwargs["json"] == {"name": "Highdeas Voice Memo Docs",
                                     "mimeType": "application/vnd.google-apps.folder"}
    # Subfolder searched for under the container, then created there.
    assert "name = '_2026_07_17_NOT_YET_PROCESSED_MUSIC'" in get.calls[1][1]["params"]["q"]
    assert "'CONTAINER_ID' in parents" in get.calls[1][1]["params"]["q"]
    assert post.calls[1][1]["json"] == {"name": "_2026_07_17_NOT_YET_PROCESSED_MUSIC",
                                        "mimeType": "application/vnd.google-apps.folder",
                                        "parents": ["CONTAINER_ID"]}
    # The doc itself, uploaded into the subfolder just created.
    doc_url, doc_kwargs = post.calls[2]
    assert doc_url == "https://www.googleapis.com/upload/drive/v3/files"
    assert doc_kwargs["params"] == {"uploadType": "multipart", "fields": "id"}
    assert doc_kwargs["headers"]["Authorization"] == "Bearer tok-123"
    assert "multipart/related; boundary=" in doc_kwargs["headers"]["Content-Type"]
    assert '"name": "Korok Dance"' in doc_kwargs["data"]
    assert '"mimeType": "application/vnd.google-apps.document"' in doc_kwargs["data"]
    assert '"parents": ["SUBFOLDER_ID"]' in doc_kwargs["data"]
    assert "<p>la la la</p>" in doc_kwargs["data"]


def test_file_doc_reuses_the_container_and_subfolder_when_both_already_exist():
    get = FakeCalls(_found("CONTAINER_ID"), _found("SUBFOLDER_ID"))
    post = FakeCalls(_created("DOC_ID"))
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs",
                          get=get, post=post, token=lambda f: "tok")

    link, needs_move = filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Korok Dance",
                                      "<p>la la la</p>")

    assert link == "https://docs.google.com/document/d/DOC_ID/edit"
    assert needs_move is False
    assert len(post.calls) == 1  # no folder-create calls, only the doc upload


def test_file_doc_blank_when_any_call_fails():
    def blowing_up(*args, **kwargs):
        raise ConnectionError("offline")

    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=blowing_up, token=lambda f: "tok")
    assert filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Title", "<p>hi</p>") == ("", False)


def test_file_doc_blank_when_the_token_cant_be_obtained_due_to_an_error():
    def blowing_up(token_file):
        raise OSError("bad token file")

    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", token=blowing_up)
    assert filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Title", "<p>hi</p>") == ("", False)


def test_file_doc_moves_the_doc_beside_the_audio_when_the_folder_can_be_found():
    # Confirmed against the real Drive API (not assumed from scope docs): a
    # files.update addParents/removeParents call succeeds for a folder the app
    # never created, even though a drive.file-scoped files.get on that same
    # folder id 404s -- see drive_write.py's module docstring.
    get = FakeCalls(_found("CONTAINER_ID"), _found("SUBFOLDER_ID"))
    post = FakeCalls(_created("DOC_ID"))
    patch = FakeCalls(FakeResponse(body={"id": "DOC_ID"}))
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, post=post, patch=patch,
                          token=lambda f: "tok-123", find_folder_id=lambda name: "AUDIO_FOLDER_ID")

    link, needs_move = filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Korok Dance",
                                      "<p>la la la</p>")

    assert link == "https://docs.google.com/document/d/DOC_ID/edit"  # unaffected by the move
    assert needs_move is False  # moved on the first try -- nothing left to reconcile
    assert len(patch.calls) == 1
    url, kwargs = patch.calls[0]
    assert url == "https://www.googleapis.com/drive/v3/files/DOC_ID"
    assert kwargs["headers"]["Authorization"] == "Bearer tok-123"
    assert kwargs["params"] == {"addParents": "AUDIO_FOLDER_ID", "removeParents": "SUBFOLDER_ID",
                                "fields": "id"}


def test_file_doc_asks_the_resolver_for_the_subfolder_name_not_the_title():
    get = FakeCalls(_found("CONTAINER_ID"), _found("SUBFOLDER_ID"))
    post = FakeCalls(_created("DOC_ID"))
    patch = FakeCalls(FakeResponse(body={"id": "DOC_ID"}))
    resolver_calls = []

    def find_folder_id(name):
        resolver_calls.append(name)
        return "AUDIO_ID"

    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, post=post, patch=patch,
                          token=lambda f: "tok", find_folder_id=find_folder_id)

    filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Korok Dance", "<p>la la la</p>")

    assert resolver_calls == ["_2026_07_17_NOT_YET_PROCESSED_MUSIC"]


def test_file_doc_skips_the_move_when_no_resolver_is_given():
    get = FakeCalls(_found("CONTAINER_ID"), _found("SUBFOLDER_ID"))
    post = FakeCalls(_created("DOC_ID"))
    patch = FakeCalls()
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, post=post, patch=patch,
                          token=lambda f: "tok")  # find_folder_id defaults to None

    link, needs_move = filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Korok Dance",
                                      "<p>la la la</p>")

    assert link == "https://docs.google.com/document/d/DOC_ID/edit"
    assert needs_move is False  # no mover configured at all -- never worth tracking for a retry
    assert patch.calls == []


def test_file_doc_needs_a_retry_when_the_resolver_finds_nothing():
    # Not yet resolvable -- likely (this being the first call of the day) the
    # audio's local folder hasn't synced up to Drive's cloud yet -- so the doc
    # stays right where it was already filed, in its own container, but is
    # flagged so a later reconciliation pass (DriveDocReconciler) retries it.
    get = FakeCalls(_found("CONTAINER_ID"), _found("SUBFOLDER_ID"))
    post = FakeCalls(_created("DOC_ID"))
    patch = FakeCalls()
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, post=post, patch=patch,
                          token=lambda f: "tok", find_folder_id=lambda name: "")

    link, needs_move = filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Korok Dance",
                                      "<p>la la la</p>")

    assert link == "https://docs.google.com/document/d/DOC_ID/edit"
    assert needs_move is True
    assert patch.calls == []


def test_file_doc_skips_the_move_when_the_target_is_already_the_current_folder():
    get = FakeCalls(_found("CONTAINER_ID"), _found("SUBFOLDER_ID"))
    post = FakeCalls(_created("DOC_ID"))
    patch = FakeCalls()
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, post=post, patch=patch,
                          token=lambda f: "tok", find_folder_id=lambda name: "SUBFOLDER_ID")

    link, needs_move = filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Korok Dance",
                                      "<p>la la la</p>")

    assert needs_move is False  # already exactly where it belongs
    assert patch.calls == []


def test_file_doc_needs_a_retry_when_the_move_itself_fails():
    # The doc file_doc already filed, and the link already returned for it,
    # must stand regardless of what happens after -- a failed move must not
    # turn a successful filing into "" and trigger the .docx fallback on top
    # of a native Doc that genuinely exists (just not yet beside the audio) --
    # but it must be flagged so a later pass tries the move again.
    get = FakeCalls(_found("CONTAINER_ID"), _found("SUBFOLDER_ID"))
    post = FakeCalls(_created("DOC_ID"))

    def blowing_up_patch(*args, **kwargs):
        raise ConnectionError("offline")

    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, post=post,
                          patch=blowing_up_patch, token=lambda f: "tok",
                          find_folder_id=lambda name: "AUDIO_FOLDER_ID")

    link, needs_move = filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Korok Dance",
                                      "<p>la la la</p>")

    assert link == "https://docs.google.com/document/d/DOC_ID/edit"
    assert needs_move is True


def test_file_doc_needs_a_retry_when_resolving_the_target_folder_raises():
    get = FakeCalls(_found("CONTAINER_ID"), _found("SUBFOLDER_ID"))
    post = FakeCalls(_created("DOC_ID"))

    def blowing_up_resolver(name):
        raise ConnectionError("offline")

    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, post=post,
                          token=lambda f: "tok", find_folder_id=blowing_up_resolver)

    link, needs_move = filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Korok Dance",
                                      "<p>la la la</p>")

    assert link == "https://docs.google.com/document/d/DOC_ID/edit"
    assert needs_move is True


# ---------------------------------------------------------------------------
# reconcile() -- retrying, later, a move file_doc's own attempt left stranded.
# ---------------------------------------------------------------------------

_LINK = "https://docs.google.com/document/d/DOC_ID/edit"


def test_reconcile_moves_a_stranded_doc_once_the_folder_can_be_found():
    get = FakeCalls(FakeResponse(body={"parents": ["SUBFOLDER_ID"]}))
    patch = FakeCalls(FakeResponse(body={"id": "DOC_ID"}))
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, patch=patch,
                          token=lambda f: "tok-123", find_folder_id=lambda name: "AUDIO_FOLDER_ID")

    moved = filer.reconcile(_LINK, "_2026_07_17_NOT_YET_PROCESSED_MUSIC")

    assert moved is True
    get_url, get_kwargs = get.calls[0]
    assert get_url == "https://www.googleapis.com/drive/v3/files/DOC_ID"
    assert get_kwargs["headers"]["Authorization"] == "Bearer tok-123"
    assert get_kwargs["params"] == {"fields": "parents"}
    patch_url, patch_kwargs = patch.calls[0]
    assert patch_url == "https://www.googleapis.com/drive/v3/files/DOC_ID"
    assert patch_kwargs["params"] == {"addParents": "AUDIO_FOLDER_ID",
                                      "removeParents": "SUBFOLDER_ID", "fields": "id"}


def test_reconcile_true_without_moving_when_already_beside_the_audio():
    # Could happen if a previous pass's move actually landed but this memo's
    # own flag never got cleared (a crash between the two, say) -- reconcile
    # must still report success so the caller clears the flag, not loop forever.
    get = FakeCalls(FakeResponse(body={"parents": ["AUDIO_FOLDER_ID"]}))
    patch = FakeCalls()
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, patch=patch,
                          token=lambda f: "tok", find_folder_id=lambda name: "AUDIO_FOLDER_ID")

    assert filer.reconcile(_LINK, "_2026_07_17_NOT_YET_PROCESSED_MUSIC") is True
    assert patch.calls == []


def test_reconcile_false_when_the_resolver_still_finds_nothing():
    get = FakeCalls()
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get,
                          token=lambda f: "tok", find_folder_id=lambda name: "")

    assert filer.reconcile(_LINK, "_2026_07_17_NOT_YET_PROCESSED_MUSIC") is False
    assert get.calls == []  # nowhere to move it -- not even worth asking where it is now


def test_reconcile_false_without_a_resolver_configured():
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", token=lambda f: "tok")

    assert filer.reconcile(_LINK, "_2026_07_17_NOT_YET_PROCESSED_MUSIC") is False


def test_reconcile_false_for_an_unparseable_link():
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", token=lambda f: "tok",
                          find_folder_id=lambda name: "AUDIO_FOLDER_ID")

    assert filer.reconcile("", "_2026_07_17_NOT_YET_PROCESSED_MUSIC") is False
    assert filer.reconcile("not a drive link", "_2026_07_17_NOT_YET_PROCESSED_MUSIC") is False


def test_reconcile_false_when_the_token_cant_be_obtained():
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", token=lambda f: "",
                          find_folder_id=lambda name: "AUDIO_FOLDER_ID")

    assert filer.reconcile(_LINK, "_2026_07_17_NOT_YET_PROCESSED_MUSIC") is False


def test_reconcile_false_when_fetching_current_parents_fails():
    def blowing_up_get(*args, **kwargs):
        raise ConnectionError("offline")

    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=blowing_up_get,
                          token=lambda f: "tok", find_folder_id=lambda name: "AUDIO_FOLDER_ID")

    assert filer.reconcile(_LINK, "_2026_07_17_NOT_YET_PROCESSED_MUSIC") is False


def test_reconcile_false_when_the_move_itself_fails():
    get = FakeCalls(FakeResponse(body={"parents": ["SUBFOLDER_ID"]}))

    def blowing_up_patch(*args, **kwargs):
        raise ConnectionError("offline")

    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, patch=blowing_up_patch,
                          token=lambda f: "tok", find_folder_id=lambda name: "AUDIO_FOLDER_ID")

    assert filer.reconcile(_LINK, "_2026_07_17_NOT_YET_PROCESSED_MUSIC") is False


# ---------------------------------------------------------------------------
# remove_if_empty() -- cleaning up a container subfolder reconcile emptied out.
# ---------------------------------------------------------------------------

def test_remove_if_empty_trashes_an_empty_subfolder():
    get = FakeCalls(_found("CONTAINER_ID"), _found("SUBFOLDER_ID"), _no_files())
    patch = FakeCalls(FakeResponse(body={"id": "SUBFOLDER_ID"}))
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, patch=patch,
                          token=lambda f: "tok-123")

    filer.remove_if_empty("_2026_07_17_NOT_YET_PROCESSED_MUSIC")

    # Container, then subfolder, searched for by name -- never created (this
    # only ever cleans up what's already there).
    assert "name = 'Highdeas Voice Memo Docs'" in get.calls[0][1]["params"]["q"]
    assert "name = '_2026_07_17_NOT_YET_PROCESSED_MUSIC'" in get.calls[1][1]["params"]["q"]
    assert "'CONTAINER_ID' in parents" in get.calls[1][1]["params"]["q"]
    # Then whatever's left inside the subfolder -- any file, not just folders.
    listing_kwargs = get.calls[2][1]
    assert listing_kwargs["params"]["q"] == "'SUBFOLDER_ID' in parents and trashed = false"
    url, kwargs = patch.calls[0]
    assert url == "https://www.googleapis.com/drive/v3/files/SUBFOLDER_ID"
    assert kwargs["headers"]["Authorization"] == "Bearer tok-123"
    assert kwargs["json"] == {"trashed": True}


def test_remove_if_empty_leaves_a_nonempty_subfolder_alone():
    get = FakeCalls(_found("CONTAINER_ID"), _found("SUBFOLDER_ID"), _found("SOME_OTHER_DOC_ID"))
    patch = FakeCalls()
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, patch=patch,
                          token=lambda f: "tok")

    filer.remove_if_empty("_2026_07_17_NOT_YET_PROCESSED_MUSIC")

    assert patch.calls == []


def test_remove_if_empty_does_nothing_when_the_container_is_missing():
    get = FakeCalls(_no_files())
    patch = FakeCalls()
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, patch=patch,
                          token=lambda f: "tok")

    filer.remove_if_empty("_2026_07_17_NOT_YET_PROCESSED_MUSIC")

    assert len(get.calls) == 1  # never even looked for the subfolder
    assert patch.calls == []


def test_remove_if_empty_does_nothing_when_the_subfolder_is_missing():
    get = FakeCalls(_found("CONTAINER_ID"), _no_files())
    patch = FakeCalls()
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=get, patch=patch,
                          token=lambda f: "tok")

    filer.remove_if_empty("_2026_07_17_NOT_YET_PROCESSED_MUSIC")

    assert len(get.calls) == 2  # never asked what's inside a subfolder that isn't there
    assert patch.calls == []


def test_remove_if_empty_swallows_a_failure():
    def blowing_up_get(*args, **kwargs):
        raise ConnectionError("offline")

    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=blowing_up_get,
                          token=lambda f: "tok")

    filer.remove_if_empty("_2026_07_17_NOT_YET_PROCESSED_MUSIC")  # must not raise


def test_remove_if_empty_does_nothing_without_a_token():
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", token=lambda f: "")

    filer.remove_if_empty("_2026_07_17_NOT_YET_PROCESSED_MUSIC")  # must not raise


# ---------------------------------------------------------------------------
# DriveDocReconciler -- the periodic pass over every memo still waiting.
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self, retired):
        self._retired = retired
        self.updates = []

    def list_retired(self):
        return list(self._retired)

    def update(self, audio_filename, **changes):
        self.updates.append((audio_filename, changes))


class FakeMemo:
    def __init__(self, audio_filename, drive_doc_needs_move, drive_doc_link="", drive_subfolder=""):
        self.audio_filename = audio_filename
        self.drive_doc_needs_move = drive_doc_needs_move
        self.drive_doc_link = drive_doc_link
        self.drive_subfolder = drive_subfolder


class FakeFiler:
    def __init__(self, reconcile_results):
        """reconcile_results: {(link, subfolder_name): bool}"""
        self._results = reconcile_results
        self.reconcile_calls = []
        self.remove_if_empty_calls = []

    def reconcile(self, link, subfolder_name):
        self.reconcile_calls.append((link, subfolder_name))
        return self._results[(link, subfolder_name)]

    def remove_if_empty(self, subfolder_name):
        self.remove_if_empty_calls.append(subfolder_name)


def test_reconciler_ignores_memos_that_dont_need_a_move():
    store = FakeStore([FakeMemo("a.m4a", drive_doc_needs_move=False)])
    filer = FakeFiler({})

    DriveDocReconciler(store, filer).run_once()

    assert filer.reconcile_calls == []
    assert store.updates == []


def test_reconciler_also_sweeps_the_subfolder_of_a_doc_that_moved_on_its_first_try():
    # file_doc's own synchronous first-try move (DriveDocFiler.file_doc) never sweeps
    # the container subfolder it leaves empty behind it -- that would cost every single
    # filing an extra Drive round trip just to clean up a subfolder the very next memo
    # that day is likely to reuse anyway. This periodic pass is where that sweep
    # actually happens, for a doc that never needed reconciling at all.
    memo = FakeMemo("a.m4a", drive_doc_needs_move=False, drive_doc_link=_LINK,
                    drive_subfolder="_2026_07_17_NOT_YET_PROCESSED_MUSIC")
    store = FakeStore([memo])
    filer = FakeFiler({})

    DriveDocReconciler(store, filer).run_once()

    assert filer.reconcile_calls == []  # already beside its audio -- nothing to retry
    assert filer.remove_if_empty_calls == ["_2026_07_17_NOT_YET_PROCESSED_MUSIC"]


def test_reconciler_clears_the_flag_and_cleans_up_once_reconcile_succeeds():
    memo = FakeMemo("a.m4a", drive_doc_needs_move=True, drive_doc_link=_LINK,
                    drive_subfolder="_2026_07_17_NOT_YET_PROCESSED_MUSIC")
    store = FakeStore([memo])
    filer = FakeFiler({(_LINK, "_2026_07_17_NOT_YET_PROCESSED_MUSIC"): True})

    DriveDocReconciler(store, filer).run_once()

    assert store.updates == [("a.m4a", {"drive_doc_needs_move": False})]
    assert filer.remove_if_empty_calls == ["_2026_07_17_NOT_YET_PROCESSED_MUSIC"]


def test_reconciler_leaves_the_flag_set_when_reconcile_still_fails():
    memo = FakeMemo("a.m4a", drive_doc_needs_move=True, drive_doc_link=_LINK,
                    drive_subfolder="_2026_07_17_NOT_YET_PROCESSED_MUSIC")
    store = FakeStore([memo])
    filer = FakeFiler({(_LINK, "_2026_07_17_NOT_YET_PROCESSED_MUSIC"): False})

    DriveDocReconciler(store, filer).run_once()

    assert store.updates == []
    assert filer.remove_if_empty_calls == []


def test_reconciler_cleans_up_each_subfolder_once_even_with_several_memos_in_it():
    link_a = "https://docs.google.com/document/d/DOC_A/edit"
    link_b = "https://docs.google.com/document/d/DOC_B/edit"
    subfolder = "_2026_07_17_NOT_YET_PROCESSED_MUSIC"
    memo_a = FakeMemo("a.m4a", drive_doc_needs_move=True, drive_doc_link=link_a,
                      drive_subfolder=subfolder)
    memo_b = FakeMemo("b.m4a", drive_doc_needs_move=True, drive_doc_link=link_b,
                      drive_subfolder=subfolder)
    store = FakeStore([memo_a, memo_b])
    filer = FakeFiler({(link_a, subfolder): True, (link_b, subfolder): True})

    DriveDocReconciler(store, filer).run_once()

    assert {name for name, _ in store.updates} == {"a.m4a", "b.m4a"}
    assert filer.remove_if_empty_calls == [subfolder]  # not once per memo


def test_reconciler_a_bad_memo_never_stops_the_rest():
    subfolder = "_2026_07_17_NOT_YET_PROCESSED_MUSIC"
    bad = FakeMemo("bad.m4a", drive_doc_needs_move=True, drive_doc_link="not a real link",
                   drive_subfolder=subfolder)
    good = FakeMemo("good.m4a", drive_doc_needs_move=True, drive_doc_link=_LINK,
                    drive_subfolder=subfolder)
    store = FakeStore([bad, good])
    filer = FakeFiler({("not a real link", subfolder): False, (_LINK, subfolder): True})

    DriveDocReconciler(store, filer).run_once()

    assert store.updates == [("good.m4a", {"drive_doc_needs_move": False})]
