import threading
from pathlib import Path

from voicememo.app import _open_when_ready, _transcribe_in_background, default_bin_dir


def test_open_when_ready_shows_the_app_only_after_the_server_is_serving():
    events = []

    class FakeWindow:
        def load_url(self, url):
            events.append(("load", url))

    _open_when_ready(FakeWindow(), "http://127.0.0.1:9/", lambda: events.append(("waited",)))

    # Wait for the server, then swap the splash for the real page — the open never
    # blocks on warming the model or transcribing the backlog.
    assert events == [("waited",), ("load", "http://127.0.0.1:9/")]


def test_transcribe_in_background_runs_refresh_off_the_calling_thread():
    ran = threading.Event()
    seen = {}

    class FakeService:
        def refresh(self):
            seen["thread"] = threading.get_ident()
            ran.set()

    _transcribe_in_background(FakeService())

    assert ran.wait(timeout=2)
    assert seen["thread"] != threading.get_ident()  # off the UI thread, so the window opens now


def test_transcribe_in_background_survives_a_failing_refresh():
    ran = threading.Event()

    class BadService:
        def refresh(self):
            ran.set()
            raise RuntimeError("boom")

    # A bad recording must never crash startup: the background thread swallows it.
    _transcribe_in_background(BadService())

    assert ran.wait(timeout=2)


def test_default_bin_dir_sits_beside_the_inbox(tmp_path):
    # The bin must live in the same parent folder as the inbox, so retiring a
    # recording (inbox -> bin) moves it *within* the same iCloud tree. Moving a
    # file out of the iCloud folder makes iCloud Drive on Windows pop a per-file
    # "move off iCloud" confirmation dialog for every Submit/Trash.
    inbox = tmp_path / "VoiceInbox"

    result = Path(default_bin_dir(str(inbox)))

    assert result == tmp_path / "VoiceBin"
    assert result.parent == inbox.parent
