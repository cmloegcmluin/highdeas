import io
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from highdeas.app import (
    PROJECT_ROOT,
    _ingest_continuously,
    _open_when_ready,
    _open_window,
    _run_browser,
    _start_upload_listener,
    _turn_on_context_menus,
    build_app,
    lexicon_path,
    platform_defaults,
    terms_source,
    build_upload_app,
    default_bin_dir,
)
from highdeas.routers import parse_choices
from highdeas.store import Memo, MemoStore
from highdeas.window_state import WindowGeometry, load_geometry, save_geometry


def test_open_when_ready_updates_then_waits_then_loads_then_restores_the_menu():
    events = []

    class FakeWindow:
        def load_url(self, url):
            events.append(("load", url))

    _open_when_ready(FakeWindow(), "http://127.0.0.1:9/", lambda: events.append(("waited",)),
                     become_current=lambda: events.append(("updated",)),
                     enable_context_menus=lambda window: events.append(("menus",)))

    # The splash is already on screen when this runs, so the launch update
    # happens behind "Loading…" instead of before any window exists — the
    # user's click always answers immediately. Then wait for the server, then
    # swap the splash for the real page, and only then hand the loaded page back
    # its right-click menu.
    assert events == [("updated",), ("waited",), ("load", "http://127.0.0.1:9/"), ("menus",)]


def test_the_right_click_menu_comes_back_on_once_the_webview_core_is_up():
    # pywebview's WebView2 backend turns the default context menus off outside
    # debug mode, which leaves the note editor's fields with nothing to cut or
    # copy from on a right-click. We turn just that setting back on.
    settings = SimpleNamespace(AreDefaultContextMenusEnabled=False)
    control = SimpleNamespace(CoreWebView2=SimpleNamespace(Settings=settings))

    _turn_on_context_menus(control)

    assert settings.AreDefaultContextMenusEnabled is True


def test_restoring_the_menu_waits_for_the_webview_core_instead_of_crashing():
    # Fired from a window whose WebView2 core hasn't finished initializing, the
    # setting has nothing to land on — so it's a quiet no-op, not a crash that
    # would take the launch down with it.
    control = SimpleNamespace(CoreWebView2=None)

    _turn_on_context_menus(control)  # must not raise


def _fake_webview(fake_window, screens=()):
    calls = []

    def create_window(title, **kwargs):
        calls.append((title, kwargs))
        return fake_window

    return SimpleNamespace(screens=list(screens), create_window=create_window), calls


def test_open_window_reopens_at_the_geometry_the_window_was_last_closed_at(tmp_path, fake_window):
    path = tmp_path / "window.json"
    save_geometry(path, WindowGeometry(width=800, height=600, x=10, y=20, maximized=False))
    webview, calls = _fake_webview(fake_window, [SimpleNamespace(x=0, y=0, width=1920, height=1080)])

    _open_window(webview, path)

    (title, kwargs), = calls
    assert title == "Highdeas"
    assert (kwargs["width"], kwargs["height"]) == (800, 600)
    assert (kwargs["x"], kwargs["y"]) == (10, 20)
    assert kwargs["maximized"] is False


def test_open_window_opens_maximized_before_anything_has_been_remembered(tmp_path, fake_window):
    webview, calls = _fake_webview(fake_window)

    _open_window(webview, tmp_path / "window.json")

    (_, kwargs), = calls
    assert kwargs["maximized"] is True


def test_open_window_forgets_a_position_no_connected_monitor_covers(tmp_path, fake_window):
    # The monitor the window was closed on is gone; opening there would put it out of reach.
    path = tmp_path / "window.json"
    save_geometry(path, WindowGeometry(x=-1900, y=0, maximized=False))
    webview, calls = _fake_webview(fake_window, [SimpleNamespace(x=0, y=0, width=1920, height=1080)])

    _open_window(webview, path)

    (_, kwargs), = calls
    assert (kwargs["x"], kwargs["y"]) == (None, None)
    # ...and closing writes back where the window actually landed — never the
    # stranded coordinates it was rescued from.
    fake_window.close()
    assert (load_geometry(path).x, load_geometry(path).y) == (0, 0)


def test_open_window_tracks_the_window_so_the_next_launch_reopens_maximized(tmp_path, fake_window):
    path = tmp_path / "window.json"
    save_geometry(path, WindowGeometry(maximized=False))
    webview, _ = _fake_webview(fake_window)

    _open_window(webview, path)
    fake_window.maximize()
    fake_window.close()

    assert load_geometry(path).maximized is True


def test_ingest_continuously_keeps_rescanning_the_inbox_off_the_calling_thread():
    stop, done, threads = threading.Event(), threading.Event(), []

    class FakeService:
        def refresh(self):
            threads.append(threading.get_ident())
            if len(threads) == 2:
                stop.set()
                done.set()

    _ingest_continuously(FakeService(), interval=0, stop=stop)

    assert done.wait(timeout=2)
    assert threads[0] != threading.get_ident()  # off the UI thread, so the window opens now
    # It scans on and on, so a memo recorded while the bin is the page on screen still
    # reaches the inbox: ingestion belongs to the app, not to whichever view is open.
    assert len(threads) == 2


def test_ingest_continuously_keeps_scanning_after_a_failing_refresh():
    stop, done, calls = threading.Event(), threading.Event(), []

    class BadService:
        def refresh(self):
            calls.append(1)
            if len(calls) == 2:
                stop.set()
                done.set()
            raise RuntimeError("boom")

    # A bad recording must never crash startup, nor end the scan that follows it.
    _ingest_continuously(BadService(), interval=0, stop=stop)

    assert done.wait(timeout=2)


def test_chrome_launcher_opens_the_url_in_the_configured_profile(monkeypatch):
    import highdeas.app as app_mod
    calls = []
    monkeypatch.setattr(app_mod.subprocess, "Popen", lambda args: calls.append(args))
    monkeypatch.setenv("HIGHDEAS_CHROME_EXE", r"C:\chrome.exe")
    monkeypatch.setenv("HIGHDEAS_CHROME_PROFILE", "Default")

    app_mod._chrome_launcher()("https://drive.google.com/x")

    # A link can't choose a Chrome profile, so the app launches Chrome pinned to it.
    assert calls == [[r"C:\chrome.exe", "--profile-directory=Default", "https://drive.google.com/x"]]


def test_drive_link_resolver_is_none_without_a_service_account_configured(monkeypatch):
    import highdeas.app as app_mod
    monkeypatch.delenv("HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)

    resolver = app_mod._drive_link_resolver("https://drive.google.com/drive/folders/PARENT_ID")

    assert resolver is None


def test_drive_link_resolver_is_none_without_a_resolvable_parent_folder_id(monkeypatch, tmp_path):
    import highdeas.app as app_mod
    monkeypatch.setenv("HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "key.json"))

    # Blank, or some other page's URL that has no Drive folder ID in it.
    assert app_mod._drive_link_resolver("") is None
    assert app_mod._drive_link_resolver("https://drive.google.com/drive/search?q=x") is None


def test_drive_link_resolver_wires_up_when_both_are_configured(monkeypatch, tmp_path):
    import highdeas.app as app_mod
    monkeypatch.setenv("HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "key.json"))

    resolver = app_mod._drive_link_resolver("https://drive.google.com/drive/folders/PARENT_ID")

    assert callable(resolver)


def test_drive_link_resolver_wires_the_service_account_file_and_parent_id_into_the_linker(monkeypatch, tmp_path):
    # Not just "truthy": the resolver's underlying DriveFolderLinker must be pointed
    # at the exact key file HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE names, and at the
    # parent ID parsed out of the folder URL -- not swapped, not defaulted, not
    # somehow pointed at the wrong folder. (DriveFolderLinker's own resolution logic,
    # including its Drive API call, is covered independently in test_drive_link.py --
    # its get/token constructor seams are where that call is mocked, since the
    # defaults this resolver leaves in place are bound once at import time and can't
    # be swapped back out from here.)
    import highdeas.app as app_mod
    key_file = tmp_path / "service-account.json"
    monkeypatch.setenv("HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE", str(key_file))

    resolver = app_mod._drive_link_resolver("https://drive.google.com/drive/folders/PARENT_ID")

    linker = resolver.__self__
    assert linker._service_account_file == str(key_file)
    assert linker._parent_id == "PARENT_ID"


def test_drive_doc_filer_is_none_without_a_token_file_configured(monkeypatch):
    import highdeas.app as app_mod
    monkeypatch.delenv("HIGHDEAS_GOOGLE_DOCS_TOKEN_FILE", raising=False)

    assert app_mod._drive_doc_filer() is None


def test_drive_doc_filer_wires_up_when_a_token_file_is_configured(monkeypatch, tmp_path):
    import highdeas.app as app_mod
    monkeypatch.setenv("HIGHDEAS_GOOGLE_DOCS_TOKEN_FILE", str(tmp_path / "token.json"))

    filer = app_mod._drive_doc_filer()

    assert callable(filer)


def test_drive_doc_filer_wires_the_token_file_and_default_container_name_into_the_filer(
        monkeypatch, tmp_path):
    # Not just "truthy": the resolver's underlying DriveDocFiler must be pointed at the
    # exact token file HIGHDEAS_GOOGLE_DOCS_TOKEN_FILE names, and (unset) the
    # documented default container -- not swapped, not blank. (DriveDocFiler's own
    # filing logic, including its Drive API calls, is covered independently in
    # test_drive_write.py -- its get/post/token constructor seams are where that's
    # mocked, since the defaults this resolver leaves in place are bound once at
    # import time and can't be swapped back out from here.)
    import highdeas.app as app_mod
    monkeypatch.delenv("HIGHDEAS_DRIVE_DOCS_FOLDER_NAME", raising=False)
    token_file = tmp_path / "token.json"
    monkeypatch.setenv("HIGHDEAS_GOOGLE_DOCS_TOKEN_FILE", str(token_file))

    filer = app_mod._drive_doc_filer()

    doc_filer = filer.__self__
    assert doc_filer._token_file == str(token_file)
    assert doc_filer._container_name == "Highdeas Voice Memo Docs"


def test_drive_doc_filer_uses_a_configured_container_name(monkeypatch, tmp_path):
    import highdeas.app as app_mod
    monkeypatch.setenv("HIGHDEAS_GOOGLE_DOCS_TOKEN_FILE", str(tmp_path / "token.json"))
    monkeypatch.setenv("HIGHDEAS_DRIVE_DOCS_FOLDER_NAME", "Voice Memo Transcripts")

    filer = app_mod._drive_doc_filer()

    assert filer.__self__._container_name == "Voice Memo Transcripts"


def test_default_bin_dir_sits_beside_the_inbox(tmp_path):
    # The bin must live in the same parent folder as the inbox, so retiring a
    # recording (inbox -> bin) moves it *within* the same iCloud tree. Moving a
    # file out of the iCloud folder makes iCloud Drive on Windows pop a per-file
    # "move off iCloud" confirmation dialog for every Submit/Trash.
    inbox = tmp_path / "Highdeas"

    result = Path(default_bin_dir(str(inbox)))

    assert result == tmp_path / "Highdeas Bin"
    assert result.parent == inbox.parent


def test_build_app_prefers_the_folder_store_and_migrates_the_db_once(tmp_path, monkeypatch):
    # HIGHDEAS_STATE_DIR is the no-special-machine switch: state moves from the
    # local SQLite file to per-memo JSONs in a folder a sync engine carries.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    state = tmp_path / "state"
    db = tmp_path / "memos.db"
    MemoStore(db).upsert(Memo(audio_filename="old.m4a", transcript="pc era", status="pending"))
    monkeypatch.setenv("HIGHDEAS_INBOX_DIR", str(inbox))
    monkeypatch.setenv("HIGHDEAS_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setenv("HIGHDEAS_DB", str(db))
    monkeypatch.setenv("HIGHDEAS_STATE_DIR", str(state))

    app, service = build_app()

    # The single-machine DB crossed into per-memo files...
    assert (state / "old.m4a.json").exists()
    # ...and the folder is live truth: an edit landing in it (as if synced in
    # from the other machine) is what the service reads back.
    data = json.loads((state / "old.m4a.json").read_text())
    data["name"] = "renamed on the other machine"
    (state / "old.m4a.json").write_text(json.dumps(data))
    assert [m.name for m in service.pending()] == ["renamed on the other machine"]


def test_build_app_sends_a_memo_to_the_asana_account_its_parent_task_names(tmp_path, monkeypatch):
    # Both Asana accounts hang off one dropdown, so the wiring has to carry a
    # second token all the way from .env to the request: a parent marked with an
    # account is created under that account's own token, never the first one's.
    import highdeas.app as app_mod

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "voice-3.m4a").write_bytes(b"AUDIO")
    monkeypatch.delenv("HIGHDEAS_STATE_DIR", raising=False)
    db_path = tmp_path / "memos.db"
    monkeypatch.setenv("HIGHDEAS_INBOX_DIR", str(inbox))
    monkeypatch.setenv("HIGHDEAS_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setenv("HIGHDEAS_DB", str(db_path))
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "MINE")
    monkeypatch.setenv("ASANA_ACCESS_TOKEN_WORK", "THEIRS")
    monkeypatch.setenv("ASANA_PARENT_TASKS", "111=Song ideas;WORK:333=Work backlog")
    sent = []

    def fake_post(url, **kwargs):
        sent.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": {}})

    real_router = app_mod.AsanaRouter
    monkeypatch.setattr(app_mod, "AsanaRouter",
                        lambda tokens, **kwargs: real_router(tokens, post=fake_post, **kwargs))

    app, _ = build_app()
    MemoStore(db_path).upsert(Memo(audio_filename="voice-3.m4a", route="asana"))
    response = app.test_client().post(
        "/submit/voice-3.m4a",
        data={"name": "Standup note", "transcript": "", "route": "asana",
              "asana_parent": "WORK:333"},
    )

    assert response.status_code == 204
    url, kwargs = sent[0]
    assert url == "https://app.asana.com/api/1.0/tasks/333/subtasks"
    assert kwargs["headers"]["Authorization"] == "Bearer THEIRS"


def test_build_app_opens_a_claude_code_session_at_the_configured_folder(tmp_path, monkeypatch):
    # The Code pane needs a directory to start in, and a voice note has none of its
    # own, so the wiring has to carry HIGHDEAS_CLAUDE_FOLDER from .env into the link.
    import highdeas.app as app_mod

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "voice-3.m4a").write_bytes(b"AUDIO")
    monkeypatch.delenv("HIGHDEAS_STATE_DIR", raising=False)
    db_path = tmp_path / "memos.db"
    monkeypatch.setenv("HIGHDEAS_INBOX_DIR", str(inbox))
    monkeypatch.setenv("HIGHDEAS_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setenv("HIGHDEAS_DB", str(db_path))
    monkeypatch.setenv("HIGHDEAS_CLAUDE_FOLDER", r"C:\projects\thing")
    opened = []
    monkeypatch.setattr(app_mod, "_deep_link_launcher", lambda: opened.append)

    app, _ = build_app()
    MemoStore(db_path).upsert(Memo(audio_filename="voice-3.m4a", route="claude"))
    response = app.test_client().post(
        "/submit/voice-3.m4a",
        data={"name": "", "transcript": "look at the scanner", "route": "claude",
              "claude_surface": "code"},
    )

    assert response.status_code == 204
    assert opened == ["claude://code/new?q=look%20at%20the%20scanner"
                      "&folder=C%3A%5Cprojects%5Cthing"]


def test_the_deep_link_launcher_hands_a_url_to_the_macs_opener(monkeypatch):
    # The Mac is the one platform this app runs on that nothing here executes: the
    # Windows desk hands claude:// to the shell itself, the Mac shells out to `open`.
    # Pin the command, so the branch that only ever runs at the other desk is at least
    # spelled right — whether Claude has registered the scheme there is that desk's own
    # answer, and only that desk can give it.
    import highdeas.app as app_mod

    monkeypatch.setattr(app_mod.sys, "platform", "darwin")
    launched = []
    monkeypatch.setattr(app_mod.subprocess, "Popen", launched.append)

    app_mod._deep_link_launcher()("claude://code/new?q=hi")

    assert launched == [["open", "claude://code/new?q=hi"]]


def test_the_built_in_model_list_runs_strongest_first():
    # The dropdown is read top-down and its first entry is the default, so the list is
    # ordered by how much model you get, not alphabetically or by release date.
    from highdeas.app import CLAUDE_MODELS

    assert [label for _, label in parse_choices(CLAUDE_MODELS)] == [
        "Fable 5", "Opus 4.8", "Sonnet 5", "Haiku 4.5"]


def test_build_app_offers_the_claude_models_the_environment_names(tmp_path, monkeypatch):
    # The model list goes stale as models come and go, so .env can replace it without
    # a code change — the same "value=Label" pairs the Asana parents are configured as.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.delenv("HIGHDEAS_STATE_DIR", raising=False)
    db_path = tmp_path / "memos.db"
    monkeypatch.setenv("HIGHDEAS_INBOX_DIR", str(inbox))
    monkeypatch.setenv("HIGHDEAS_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setenv("HIGHDEAS_DB", str(db_path))
    monkeypatch.setenv("HIGHDEAS_CLAUDE_MODELS", "claude-sonnet-5=Sonnet 5;claude-opus-4-8=Opus")

    app, _ = build_app()
    MemoStore(db_path).upsert(Memo(audio_filename="voice-3.m4a", route="claude"))
    body = app.test_client().get("/").data.decode()

    assert '<option value="claude-sonnet-5" >Sonnet 5&nbsp;</option>' in body
    assert '<option value="claude-opus-4-8" >Opus&nbsp;</option>' in body


def test_build_app_reads_blank_claude_settings_as_unset(tmp_path, monkeypatch):
    # .env.example ships both keys present and empty, which is how this project spells
    # "not set" — so a blank must fall back to the built-in list and this checkout, not
    # leave an empty dropdown and a session opening in the home directory.
    import highdeas.app as app_mod

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "voice-3.m4a").write_bytes(b"AUDIO")
    monkeypatch.delenv("HIGHDEAS_STATE_DIR", raising=False)
    db_path = tmp_path / "memos.db"
    monkeypatch.setenv("HIGHDEAS_INBOX_DIR", str(inbox))
    monkeypatch.setenv("HIGHDEAS_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setenv("HIGHDEAS_DB", str(db_path))
    monkeypatch.setenv("HIGHDEAS_CLAUDE_MODELS", "")
    monkeypatch.setenv("HIGHDEAS_CLAUDE_FOLDER", "")
    opened = []
    monkeypatch.setattr(app_mod, "_deep_link_launcher", lambda: opened.append)

    app, _ = build_app()
    MemoStore(db_path).upsert(Memo(audio_filename="voice-3.m4a", route="claude"))
    body = app.test_client().get("/").data.decode()
    app.test_client().post("/submit/voice-3.m4a",
                           data={"name": "", "transcript": "hi", "route": "claude",
                                 "claude_surface": "code"})

    assert "Opus 4.8&nbsp;" in body
    assert opened == ["claude://code/new?q=hi&folder="
                      + quote(str(app_mod.PROJECT_ROOT), safe="")]


def test_build_app_reads_every_folder_from_the_environment(tmp_path, monkeypatch):
    inbox, bin_dir, drive = tmp_path / "inbox", tmp_path / "bin", tmp_path / "drive"
    inbox.mkdir()
    # Not delenv: build_app() calls load_dotenv(), which repopulates any var that's
    # genuinely absent from os.environ from this checkout's real .env — on a machine
    # where HIGHDEAS_STATE_DIR is configured (as Douglas's now is), delenv here would
    # silently point this test's store at his real state folder instead of tmp_path.
    # An empty value already "present" blocks that: load_dotenv() never overrides a
    # key that's already set, per its own override=False default (main.py: "if k in
    # os.environ and not self.override: <skip>") — and "" already reads as unset to
    # _build_store (`if not state_dir`), so the intended SQLite-mode behavior holds.
    monkeypatch.setenv("HIGHDEAS_STATE_DIR", "")
    db_path = tmp_path / "memos.db"
    monkeypatch.setenv("HIGHDEAS_INBOX_DIR", str(inbox))
    monkeypatch.setenv("HIGHDEAS_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("HIGHDEAS_DB", str(db_path))
    monkeypatch.setenv("HIGHDEAS_DRIVE_BASE", str(drive))
    (inbox / "voice-3.m4a").write_bytes(b"AUDIO")

    app, _ = build_app()
    MemoStore(db_path).upsert(Memo(audio_filename="voice-3.m4a", route="drive"))
    response = app.test_client().post(
        "/submit/voice-3.m4a", data={"name": "Korok", "transcript": "", "route": "drive"}
    )

    # Submitting a Drive memo walks all four configured folders at once: it reads the
    # audio from the inbox, files a copy under the Drive base, retires the original to
    # the bin, and marks the row processed in the database the test seeded.
    assert response.status_code == 204
    today = datetime.now().strftime("%Y_%m_%d")
    assert (drive / f"_{today}_NOT_YET_PROCESSED_MUSIC" / "Korok.m4a").read_bytes() == b"AUDIO"
    assert (bin_dir / "voice-3.m4a").read_bytes() == b"AUDIO"
    assert not (inbox / "voice-3.m4a").exists()
    assert MemoStore(db_path).get("voice-3.m4a").status == "processed"


class FakeUploadService:
    """The two things the upload app asks of the inbox service."""

    def __init__(self, knows=False):
        self._knows = knows
        self.refreshed = threading.Event()
        self.waited = None

    def knows(self, audio_filename):
        return self._knows

    def refresh(self, wait=False, adopt_now=None):
        self.waited = wait
        self.adopted_now = adopt_now
        self.refreshed.set()


def test_build_upload_app_is_off_until_a_token_is_configured(monkeypatch):
    # Without a shared token there is nothing safe to expose to the LAN.
    monkeypatch.delenv("HIGHDEAS_UPLOAD_TOKEN", raising=False)

    assert build_upload_app(FakeUploadService()) is None


def test_build_upload_app_accepts_a_recording_and_kicks_off_ingest(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    monkeypatch.setenv("HIGHDEAS_INBOX_DIR", str(inbox))
    monkeypatch.setenv("HIGHDEAS_UPLOAD_TOKEN", "tok")
    service = FakeUploadService()

    app = build_upload_app(service)
    response = app.test_client().post(
        "/upload",
        data={"audio": (io.BytesIO(b"RIFFxxxxWAVE"), "take.wav")},
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 201
    assert (inbox / response.get_json()["stored"]).read_bytes() == b"RIFFxxxxWAVE"
    # Adoption doesn't wait for an open inbox page's poll: the upload triggers
    # a refresh itself — off the request thread, so the 2xx isn't held hostage
    # to transcription. And it must be a *waiting* refresh: during a burst the
    # in-flight scan predates this file, and a skipped trigger never recurs.
    assert service.refreshed.wait(timeout=2)
    assert service.waited is True
    # The refresh names the key it just landed, so the wait-for-state settle
    # (for audio synced in from the other machine) never delays a phone push.
    assert service.adopted_now == response.get_json()["stored"]


def test_build_upload_app_confirms_an_already_processed_recording_without_reingesting(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HIGHDEAS_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("HIGHDEAS_UPLOAD_TOKEN", "tok")
    service = FakeUploadService(knows=True)

    app = build_upload_app(service)
    response = app.test_client().post(
        "/upload",
        data={"audio": (io.BytesIO(b"RIFFxxxxWAVE"), "take.wav")},
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 200
    assert not service.refreshed.is_set()


def test_start_upload_listener_serves_the_upload_app_to_the_lan(monkeypatch):
    monkeypatch.setenv("HIGHDEAS_UPLOAD_PORT", "5155")
    served = threading.Event()
    bound = {}

    class FakeApp:
        def run(self, host, port, threaded, use_reloader):
            bound.update(host=host, port=port)
            served.set()

    _start_upload_listener(FakeApp())

    # Off the calling thread (both UI modes block their own thread on their own
    # server), reachable from the LAN, on the configured port.
    assert served.wait(timeout=2)
    assert bound == {"host": "0.0.0.0", "port": 5155}


def test_start_upload_listener_stays_dark_without_an_upload_app():
    _start_upload_listener(None)  # no token configured: nothing must listen


def test_a_missing_token_is_announced_not_just_printed():
    # pythonw has no console: print alone left a healthy-looking window with
    # a silently deaf port. The announcement is what reaches the page.
    said = []

    _start_upload_listener(None, uploads_off=said.append)

    assert said and "HIGHDEAS_UPLOAD_TOKEN" in said[0]


def test_the_listener_outlives_a_bind_race_and_clears_its_warning(monkeypatch):
    # The update respawn races the dying parent for the port: the child can
    # try to bind while the parent still holds it. One lost race must not
    # leave this machine deaf to the phone until the next cold start.
    monkeypatch.setenv("HIGHDEAS_UPLOAD_PORT", "5155")
    said = []
    serving = threading.Event()
    attempts = []

    class FakeApp:
        def run(self, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("Address already in use")
            serving.set()
            threading.Event().wait()  # block for the process lifetime, like a real server

    _start_upload_listener(FakeApp(), uploads_off=said.append,
                           retry_delay=0.05, all_clear_after=0.05)

    assert serving.wait(timeout=2)
    deadline = time.monotonic() + 2
    while (not said or said[-1] is not None) and time.monotonic() < deadline:
        time.sleep(0.01)
    # The lost race was announced, and the win that followed withdrew it.
    assert said[0] is not None and "5155" in said[0]
    assert said[-1] is None


def test_a_malformed_upload_port_disables_the_listener_not_the_app(monkeypatch, capsys):
    # Every other env misconfiguration in this app degrades gracefully; a
    # typo'd port must not take the window down with it (invisibly, under
    # the pythonw taskbar launch).
    monkeypatch.setenv("HIGHDEAS_UPLOAD_PORT", "5,055")
    ran = []

    class FakeApp:
        def run(self, **kwargs):
            ran.append(kwargs)

    _start_upload_listener(FakeApp())

    assert ran == []
    assert "HIGHDEAS_UPLOAD_PORT" in capsys.readouterr().out


def test_a_failed_bind_reports_itself_instead_of_dying_silently(monkeypatch, capsys):
    # A second Highdeas instance (or another app on the port) would otherwise
    # leave the window working and the phone retrying forever with no clue.
    monkeypatch.setenv("HIGHDEAS_UPLOAD_PORT", "5155")
    failed = threading.Event()

    class FakeApp:
        def run(self, **kwargs):
            try:
                raise OSError("Address already in use")
            finally:
                failed.set()

    _start_upload_listener(FakeApp())

    assert failed.wait(timeout=2)
    deadline = time.monotonic() + 2
    out = ""
    while "upload listener" not in out and time.monotonic() < deadline:
        out += capsys.readouterr().out
        time.sleep(0.02)
    assert "upload listener" in out


def test_main_starts_the_upload_listener_before_the_blocking_ui(monkeypatch):
    import highdeas.app as app_mod
    order = []
    monkeypatch.setenv("HIGHDEAS_DESKTOP", "0")
    monkeypatch.setattr(app_mod, "_set_windows_app_id", lambda: None)
    monkeypatch.setattr(app_mod, "build_app", lambda: ("APP", "SERVICE"))
    monkeypatch.setattr(app_mod, "_ingest_continuously", lambda service: None)
    monkeypatch.setattr(app_mod, "build_upload_app", lambda service: ("UPLOAD-FOR", service))
    monkeypatch.setattr(app_mod, "_start_upload_listener",
                        lambda app, uploads_off: order.append(("upload", app)))
    monkeypatch.setattr(app_mod, "_run_browser", lambda app: order.append(("ui", app)))

    app_mod.main()

    # _run_browser (and the desktop path) block for the process lifetime, so
    # the listener must be up first — "after the UI" would mean never.
    assert order == [("upload", ("UPLOAD-FOR", "SERVICE")), ("ui", "APP")]


def test_main_routes_a_dead_listener_onto_the_page(monkeypatch):
    import highdeas.app as app_mod

    class FakeFlask:
        def __init__(self):
            self.config = {}

    web_app = FakeFlask()
    monkeypatch.setenv("HIGHDEAS_DESKTOP", "0")
    monkeypatch.setattr(app_mod, "_set_windows_app_id", lambda: None)
    monkeypatch.setattr(app_mod, "build_app", lambda: (web_app, "SERVICE"))
    monkeypatch.setattr(app_mod, "_ingest_continuously", lambda service: None)
    monkeypatch.setattr(app_mod, "build_upload_app", lambda service: None)  # no token
    monkeypatch.setattr(app_mod, "_run_browser", lambda app: None)

    app_mod.main()

    # The page is the one place this machine gets to say it can't hear the
    # phone — pythonw has no console for the print to reach.
    assert "HIGHDEAS_UPLOAD_TOKEN" in web_app.config["PHONE_UPLOADS_OFF"]


def test_run_browser_serves_on_the_configured_port_without_opening_a_browser(monkeypatch):
    monkeypatch.setenv("HIGHDEAS_PORT", "5123")
    monkeypatch.setenv("HIGHDEAS_OPEN_BROWSER", "0")
    served = []

    class FakeApp:
        def run(self, port):
            served.append(port)

    _run_browser(FakeApp())

    # Browser-mode fallback: the port is honoured, and nothing pops a tab open.
    assert served == [5123]


def test_main_falls_back_to_the_browser_when_the_desktop_window_is_switched_off(monkeypatch):
    import highdeas.app as app_mod
    opened = []
    # A config dict, not a bare string: main() plants the phone-uploads
    # warning in app.config when the token is missing (as it is here).
    web_app = SimpleNamespace(config={})
    monkeypatch.setenv("HIGHDEAS_DESKTOP", "0")
    monkeypatch.delenv("HIGHDEAS_UPLOAD_TOKEN", raising=False)
    monkeypatch.setattr(app_mod, "_set_windows_app_id", lambda: None)
    monkeypatch.setattr(app_mod, "build_app", lambda: (web_app, "SERVICE"))
    monkeypatch.setattr(app_mod, "_ingest_continuously", lambda service: None)
    monkeypatch.setattr(app_mod, "_run_desktop", lambda app: opened.append(("desktop", app)) or True)
    monkeypatch.setattr(app_mod, "_run_browser", lambda app: opened.append(("browser", app)))

    app_mod.main()

    # The documented escape hatch: HIGHDEAS_DESKTOP=0 must never reach the native window,
    # even though _run_desktop would have succeeded had it been asked.
    assert opened == [("browser", web_app)]


def test_set_windows_app_id_uses_the_app_id_the_shortcut_carries(monkeypatch):
    import ctypes

    import highdeas.app as app_mod

    calls = []

    class FakeShell32:
        def SetCurrentProcessExplicitAppUserModelID(self, app_id):
            calls.append(app_id)

    class FakeWinDLL:
        shell32 = FakeShell32()

    monkeypatch.setattr(ctypes, "windll", FakeWinDLL(), raising=False)

    app_mod._set_windows_app_id()

    # Windows only merges the running window into the pinned Highdeas.lnk when this
    # process AUMID exactly equals the shortcut's System.AppUserModel.ID. Pin the two
    # values together here: if the app or Create-HighdeasShortcut.ps1 changes it, the
    # taskbar silently regresses to pythonw.exe's generic python icon.
    assert calls == ["Douglas.Highdeas"]
    assert app_mod.APP_ID == "Douglas.Highdeas"


def test_platform_defaults_windows_keeps_the_original_paths():
    defaults = platform_defaults("win32")

    assert defaults.inbox == r"C:\Users\Douglas\iCloudDrive\iCloud~is~workflow~my~workflows\Highdeas"
    assert defaults.chrome == r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    assert defaults.drive_base == r"G:\My Drive\voice memos (top level)"


def test_platform_defaults_mac_points_at_mac_realities(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    defaults = platform_defaults("darwin")

    # The Shortcut's iCloud container as macOS mounts it, and Chrome's binary
    # inside its app bundle. Drive's mount varies per account; .env carries
    # the real value — the default just has to be somewhere sane under HOME.
    assert defaults.inbox == str(
        tmp_path / "Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Highdeas")
    assert defaults.chrome == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    assert defaults.drive_base.startswith(str(tmp_path))


def test_build_app_and_upload_app_read_defaults_through_the_platform(monkeypatch, tmp_path):
    # Whatever the platform, the env always wins — pin that the refactor kept
    # the environment override path intact end to end.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setenv("HIGHDEAS_INBOX_DIR", str(inbox))
    monkeypatch.setenv("HIGHDEAS_UPLOAD_TOKEN", "tok")

    app = build_upload_app(FakeUploadService())
    response = app.test_client().post(
        "/upload",
        data={"audio": (io.BytesIO(b"RIFFxxxxWAVE"), "take.wav")},
        headers={"Authorization": "Bearer tok"},
    )

    assert response.status_code == 201
    assert list(inbox.iterdir())


def test_the_splash_wears_the_apps_own_system_colors():
    # The page paints with the system Canvas (adapting to light/dark); a
    # hardcoded slate splash reads as a different app for the first second.
    from highdeas.app import _SPLASH_HTML
    assert "color-scheme: light dark" in _SPLASH_HTML
    assert "background: Canvas" in _SPLASH_HTML
    assert "#0f172a" not in _SPLASH_HTML


def test_the_lexicon_sits_beside_the_state_both_machines_share(tmp_path, monkeypatch):
    # The terms are his, not this PC's: a name taught to one machine has to reach the
    # other. The shared state folder is the one place both already read.
    monkeypatch.setenv("HIGHDEAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HIGHDEAS_LEXICON", raising=False)

    assert lexicon_path() == tmp_path / "state" / "lexicon.md"


def test_the_lexicon_can_live_wherever_he_already_keeps_such_a_list(tmp_path, monkeypatch):
    monkeypatch.setenv("HIGHDEAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HIGHDEAS_LEXICON", str(tmp_path / "notes" / "my terms.md"))

    assert lexicon_path() == tmp_path / "notes" / "my terms.md"


def test_a_lone_machines_lexicon_sits_in_its_checkout(monkeypatch):
    monkeypatch.delenv("HIGHDEAS_STATE_DIR", raising=False)
    monkeypatch.delenv("HIGHDEAS_LEXICON", raising=False)

    assert lexicon_path().name == "lexicon.md"
    assert lexicon_path().parent == PROJECT_ROOT


def test_build_app_reads_every_transcription_against_the_lexicon_as_it_stands(tmp_path, monkeypatch):
    # The terms have to actually reach the transcriber — and be re-read as the list
    # grows, so a name taught at nine o'clock fixes the memo recorded at five past.
    import highdeas.app as app_mod
    inbox, state = tmp_path / "inbox", tmp_path / "state"
    inbox.mkdir()
    state.mkdir()
    monkeypatch.setenv("HIGHDEAS_INBOX_DIR", str(inbox))
    monkeypatch.setenv("HIGHDEAS_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setenv("HIGHDEAS_DB", str(tmp_path / "memos.db"))
    monkeypatch.setenv("HIGHDEAS_STATE_DIR", str(state))
    monkeypatch.delenv("HIGHDEAS_LEXICON", raising=False)
    monkeypatch.delenv("HIGHDEAS_NAMES_SHEET", raising=False)
    built = {}
    monkeypatch.setattr(app_mod, "Transcriber", lambda **kwargs: built.update(kwargs))

    build_app()
    (state / "lexicon.md").write_text("Sagittal\n", encoding="utf-8")

    assert built["read_terms"]() == ("Sagittal",)


def test_the_terms_are_the_lexicon_alone_until_a_source_is_listed(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "lexicon.md").write_text("Sagittal\n", encoding="utf-8")
    monkeypatch.setenv("HIGHDEAS_STATE_DIR", str(state))
    monkeypatch.delenv("HIGHDEAS_LEXICON", raising=False)

    assert terms_source()() == ("Sagittal",)


def test_the_names_in_a_listed_sheet_are_terms_besides_the_ones_in_the_lexicon(tmp_path, monkeypatch):
    # The sheets to read are a list kept beside the lexicon, in the folder both
    # machines share — one more line adds one more source, with nothing to configure
    # and nothing to restart, because there will be many of them.
    import highdeas.app as app_mod
    state = tmp_path / "state"
    state.mkdir()
    (state / "lexicon.md").write_text("Sagittal\n", encoding="utf-8")
    (state / "lexicon-sources.md").write_text(
        "# the people I see\n"
        "https://docs.google.com/spreadsheets/d/SHEET_ID/edit?usp=drivesdk C2:C\n",
        encoding="utf-8")
    monkeypatch.setenv("HIGHDEAS_STATE_DIR", str(state))
    monkeypatch.delenv("HIGHDEAS_LEXICON", raising=False)
    monkeypatch.setenv("HIGHDEAS_GOOGLE_KEY", str(tmp_path / "robot.json"))
    asked = {}

    def fetch(session, *, spreadsheet, cell_range):
        asked.update(session=session, spreadsheet=spreadsheet, cell_range=cell_range)
        return ("Marguerite", "Sasha")

    monkeypatch.setattr(app_mod, "authorized_session", lambda key: ("SIGNED", key))
    monkeypatch.setattr(app_mod, "fetch_names", fetch)

    assert terms_source()() == ("Sagittal", "Marguerite", "Sasha")
    # The link is what's to hand, so the id comes out of it; the cells are the line's.
    assert asked["spreadsheet"] == "SHEET_ID"
    assert asked["cell_range"] == "C2:C"
    # One key opens every sheet on the list.
    assert asked["session"] == ("SIGNED", str(tmp_path / "robot.json"))
