"""Entrypoint: build the real review service and run the local web app."""
import os
import socket
import threading
import time
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

from voicememo.routers import DriveMusicRouter, NotesnookRouter, Router
from voicememo.service import ReviewService
from voicememo.store import MemoStore
from voicememo.transcribe import Transcriber
from voicememo.web import create_app

DEFAULT_INBOX = r"C:\Users\Douglas\iCloudDrive\iCloud~is~workflow~my~workflows\VoiceInbox"
DEFAULT_DRIVE_BASE = r"G:\My Drive\voice memos (top level)"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_bin_dir(inbox_dir):
    """Where retired recordings go, kept beside the inbox on purpose.

    Submit and Trash move a recording from the inbox to the bin. If the bin sits
    outside the inbox's iCloud folder, that move drags the file off iCloud, and
    iCloud Drive on Windows pops a per-file "move to this PC" confirmation for
    every action — and a cancelled/hung move leaves the file behind to be
    re-ingested. Keeping the bin a sibling of the inbox means the move stays
    within iCloud: silent, and it actually completes."""
    return str(Path(inbox_dir).parent / "VoiceBin")

APP_NAME = "Highdeas"
APP_ICON = PROJECT_ROOT / "voicememo.ico"

# Shown instantly in the native window for the brief moment before the local server
# accepts connections, so the user never stares at a blank frame. Self-contained; the
# dark slate matches the window background below so there's no white flash on open.
_SPLASH_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><style>
  html, body { height: 100%; margin: 0; }
  body { display: flex; align-items: center; justify-content: center;
         background: #0f172a; color: #e2e8f0;
         font-family: -apple-system, "Segoe UI", system-ui, sans-serif; }
  .box { text-align: center; }
  .name { font-size: 1.9rem; font-weight: 650; letter-spacing: .01em; }
  .sub { margin-top: .55rem; font-size: .85rem; opacity: .6; }
  .spin { width: 34px; height: 34px; margin: 1.5rem auto 0; border-radius: 50%;
          border: 3px solid rgba(148, 163, 184, .25); border-top-color: #22c55e;
          animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style></head>
<body><div class="box">
  <div class="name">Highdeas</div>
  <div class="spin"></div>
  <div class="sub">Loading…</div>
</div></body></html>
"""


def build_app():
    load_dotenv(PROJECT_ROOT / ".env")
    inbox_dir = os.environ.get("VOICE_INBOX_DIR", DEFAULT_INBOX)
    db_path = os.environ.get("VOICE_DB", str(PROJECT_ROOT / "memos.db"))
    bin_dir = os.environ.get("VOICE_BIN_DIR", default_bin_dir(inbox_dir))
    drive_base = os.environ.get("VOICE_DRIVE_BASE", DEFAULT_DRIVE_BASE)
    notesnook = NotesnookRouter(os.environ.get("NOTESNOOK_INBOX_API_KEY", ""))
    drive = DriveMusicRouter(inbox_dir, drive_base)
    transcriber = Transcriber()
    service = ReviewService(
        inbox_dir=inbox_dir,
        store=MemoStore(db_path),
        transcriber=transcriber,
        bin_dir=bin_dir,
        route=Router(notesnook=notesnook, drive=drive),
    )
    app = create_app(service, inbox_dir=inbox_dir, bin_dir=bin_dir, drive_dir=drive_base)
    return app, service


def main():
    _set_windows_app_id()
    app, service = build_app()
    _transcribe_in_background(service)
    if os.environ.get("VOICE_DESKTOP", "1") == "1" and _run_desktop(app):
        return
    _run_browser(app)


def _transcribe_in_background(service):
    """Catch up on any waiting recordings off the UI thread, so the window opens
    instantly and memos stream in as they finish transcribing (the /pending poll
    surfaces them). A bad recording must never crash startup, so swallow errors."""
    def run():
        try:
            service.refresh()
        except Exception as exc:  # noqa: BLE001 — startup survives a bad recording
            print(f"Background transcription failed ({exc}).")

    threading.Thread(target=run, daemon=True, name="highdeas-transcribe").start()


def _run_desktop(app):
    """Open the app in its own native window (Edge WebView2), showing a splash only
    until the local server accepts connections. Returns False (so main falls back to
    the browser) if pywebview is unavailable."""
    try:
        import webview
    except Exception as exc:  # noqa: BLE001 — no GUI backend; fall back to the browser
        print(f"Native window unavailable ({exc}); opening in the browser instead.")
        return False

    port = _free_port()
    threading.Thread(
        target=lambda: app.run(port=port, threaded=True, use_reloader=False),
        daemon=True,
    ).start()

    try:
        window = webview.create_window(
            APP_NAME, html=_SPLASH_HTML, width=1360, height=900, background_color="#0f172a",
        )
        url = f"http://127.0.0.1:{port}/"
        # pywebview 6's winforms backend applies this to the window (and thus the taskbar
        # button); without it Windows shows pythonw.exe's icon. The docstring's "GTK/QT
        # only" note is stale — see platforms/winforms.py.
        icon = str(APP_ICON) if APP_ICON.is_file() else None
        webview.start(
            lambda: _open_when_ready(window, url, lambda: _wait_until_serving(port)),
            icon=icon,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — any failure should fall back to the browser
        print(f"Native window unavailable ({exc}); opening in the browser instead.")
        return False


def _open_when_ready(window, url, wait_until_ready):
    """Wait for the local server, then swap the splash for the real app. The model
    load and backlog transcription happen in the background, never on this path."""
    wait_until_ready()
    window.load_url(url)


def _run_browser(app):
    port = int(os.environ.get("VOICE_PORT", "5000"))
    if os.environ.get("VOICE_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    app.run(port=port)


def _free_port():
    """An OS-assigned free loopback port for the local server behind the native window."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_serving(port, *, timeout=30.0):
    """Block until the local server accepts connections (it starts almost instantly)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _set_windows_app_id():
    """Give the app its own taskbar identity so Windows shows its icon, not python's."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Douglas.Highdeas")
    except Exception:  # noqa: BLE001 — non-Windows or unavailable; harmless
        pass


if __name__ == "__main__":
    main()
