"""Tests for filing a memo's transcript into Google Drive as a real, native
Google Doc -- via the actual Drive API, authenticated as Douglas's own Google
account rather than a service account (see drive_write.py's module docstring
for why a service account can't own a file in a personal Drive)."""
from highdeas.drive_write import TOKEN_SCOPE, DriveDocFiler, _user_access_token


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
    assert filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Title", "<p>hi</p>") == ""


def test_file_doc_blank_without_a_subfolder_name_or_title():
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", token=lambda f: "tok")
    assert filer.file_doc("", "Title", "<p>hi</p>") == ""
    assert filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "", "<p>hi</p>") == ""


def test_file_doc_blank_when_the_token_cant_be_obtained():
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", token=lambda f: "")
    assert filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Title", "<p>hi</p>") == ""


def test_file_doc_creates_the_container_and_subfolder_when_neither_exists_yet():
    get = FakeCalls(_no_files(), _no_files())
    post = FakeCalls(_created("CONTAINER_ID"), _created("SUBFOLDER_ID"), _created("DOC_ID"))
    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs",
                          get=get, post=post, token=lambda f: "tok-123")

    link = filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Korok Dance", "<p>la la la</p>")

    assert link == "https://docs.google.com/document/d/DOC_ID/edit"
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

    link = filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Korok Dance", "<p>la la la</p>")

    assert link == "https://docs.google.com/document/d/DOC_ID/edit"
    assert len(post.calls) == 1  # no folder-create calls, only the doc upload


def test_file_doc_blank_when_any_call_fails():
    def blowing_up(*args, **kwargs):
        raise ConnectionError("offline")

    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", get=blowing_up, token=lambda f: "tok")
    assert filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Title", "<p>hi</p>") == ""


def test_file_doc_blank_when_the_token_cant_be_obtained_due_to_an_error():
    def blowing_up(token_file):
        raise OSError("bad token file")

    filer = DriveDocFiler("token.json", "Highdeas Voice Memo Docs", token=blowing_up)
    assert filer.file_doc("_2026_07_17_NOT_YET_PROCESSED_MUSIC", "Title", "<p>hi</p>") == ""
