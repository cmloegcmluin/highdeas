"""Entrypoint: build the real inbox service and run the local web app."""
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from highdeas.routers import AsanaRouter, DriveMusicRouter, NotesnookRouter, Router, parse_asana_parents
from highdeas.service import InboxService
from highdeas.store import FolderStore, MemoStore, adopt_legacy_db
from highdeas.transcribe import Transcriber
from highdeas.upload import create_upload_app
from highdeas.web import create_app
from highdeas.window_state import WindowGeometryTracker, load_geometry

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PlatformDefaults:
    """Fallback paths when `.env` doesn't say — different realities per OS."""
    inbox: str
    chrome: str
    drive_base: str


def platform_defaults(platform=None):
    """The default paths for this OS. The env always wins; these only answer
    when `.env` is silent. Windows keeps the paths the app grew up with; on
    macOS the Shortcut's iCloud container and Chrome's bundle binary live in
    their Apple-decreed places (Drive's mount varies per account, so its
    default is merely somewhere sane under HOME)."""
    if (platform or sys.platform) == "darwin":
        home = Path(os.environ.get("HOME", Path.home()))
        return PlatformDefaults(
            inbox=str(home / "Library/Mobile Documents/iCloud~is~workflow~my~workflows"
                             "/Documents/Highdeas"),
            chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            drive_base=str(home / "Google Drive/voice memos (top level)"),
        )
    return PlatformDefaults(
        inbox=r"C:\Users\Douglas\iCloudDrive\iCloud~is~workflow~my~workflows\Highdeas",
        chrome=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        drive_base=r"G:\My Drive\voice memos (top level)",
    )


def default_bin_dir(inbox_dir):
    """Where retired recordings go, kept beside the inbox on purpose.

    Submit and Trash move a recording from the inbox to the bin. If the bin sits
    outside the inbox's iCloud folder, that move drags the file off iCloud, and
    iCloud Drive on Windows pops a per-file "move to this PC" confirmation for
    every action — and a cancelled/hung move leaves the file behind to be
    re-ingested. Keeping the bin a sibling of the inbox means the move stays
    within iCloud: silent, and it actually completes."""
    return str(Path(inbox_dir).parent / "Highdeas Bin")

APP_NAME = "Highdeas"
# Windows taskbar identity. Must stay byte-for-byte identical to the
# System.AppUserModel.ID that "Create-HighdeasShortcut.ps1" stamps on Highdeas.lnk:
# Windows only merges this app's window into the pinned shortcut when the two match.
# If they drift, the taskbar falls back to pythonw.exe's generic python icon.
APP_ID = "Douglas.Highdeas"
APP_ICON = PROJECT_ROOT / "highdeas.ico"
# Where the window's size, position, and maximized state are remembered between launches.
WINDOW_STATE = PROJECT_ROOT / "window.json"

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
    inbox_dir = os.environ.get("HIGHDEAS_INBOX_DIR", platform_defaults().inbox)
    db_path = os.environ.get("HIGHDEAS_DB", str(PROJECT_ROOT / "memos.db"))
    bin_dir = os.environ.get("HIGHDEAS_BIN_DIR", default_bin_dir(inbox_dir))
    store = _build_store(db_path)
    drive_base = os.environ.get("HIGHDEAS_DRIVE_BASE", platform_defaults().drive_base)
    notesnook = NotesnookRouter(os.environ.get("NOTESNOOK_INBOX_API_KEY", ""))
    drive = DriveMusicRouter(inbox_dir, drive_base)
    asana_parents = parse_asana_parents(os.environ.get("ASANA_PARENT_TASKS", ""))
    asana = AsanaRouter(
        os.environ.get("ASANA_ACCESS_TOKEN", ""),
        default_parent=asana_parents[0][0] if asana_parents else "",
    )
    transcriber = Transcriber()
    service = InboxService(
        inbox_dir=inbox_dir,
        store=store,
        transcriber=transcriber,
        bin_dir=bin_dir,
        route=Router(notesnook=notesnook, drive=drive, asana=asana),
    )
    app = create_app(service, inbox_dir=inbox_dir, bin_dir=bin_dir,
                     open_link=_chrome_launcher(), asana_parents=asana_parents)
    return app, service


def _build_store(db_path):
    """The memo store this machine runs on. HIGHDEAS_STATE_DIR is the
    no-special-machine switch: set, state lives as per-memo files in a folder
    a sync engine shares between machines (any memos in the old local DB are
    carried across on first boot); unset, the single-machine SQLite remains."""
    state_dir = os.environ.get("HIGHDEAS_STATE_DIR", "")
    if not state_dir:
        return MemoStore(db_path)
    store = FolderStore(state_dir)
    adopted = adopt_legacy_db(db_path, store)
    if adopted:
        print(f"Adopted {adopted} memos from {db_path} into {state_dir}.")
    return store


def build_upload_app(service):
    """The LAN-facing upload app the phone pushes recordings to, or None until
    an upload token is configured — without a shared token there is nothing
    safe to expose to the network."""
    token = os.environ.get("HIGHDEAS_UPLOAD_TOKEN", "")
    if not token:
        return None
    return create_upload_app(
        inbox_dir=os.environ.get("HIGHDEAS_INBOX_DIR", platform_defaults().inbox),
        token=token,
        is_known=service.knows,
        # Adoption shouldn't wait for the scanner's next pass — but
        # transcription is slow, so refresh off the request thread and let
        # the 2xx return the moment the file is in place.
        on_received=lambda key: _refresh_when_free(service, adopt_now=key),
    )


def _refresh_when_free(service, adopt_now=None):
    """Refresh off the request thread, *waiting* for any scan already running:
    during a burst of uploads the in-flight scan snapshotted the inbox before
    this recording landed, and the upload's trigger fires exactly once — a
    skipped (non-blocking) refresh would leave the file to the scanner's next
    pass. Swallow errors like the scanner does."""
    def run():
        try:
            service.refresh(wait=True, adopt_now=adopt_now)
        except Exception as exc:  # noqa: BLE001 — a bad recording must not kill the thread
            print(f"Post-upload refresh failed ({exc}).")

    threading.Thread(target=run, daemon=True, name="highdeas-upload-refresh").start()


def _chrome_launcher():
    """Return a callable that opens a URL in a specific Chrome profile. Drive is
    signed into the wanted Google account only in that profile, and a link can't
    choose one, so launch Chrome directly with --profile-directory."""
    chrome = os.environ.get("HIGHDEAS_CHROME_EXE", platform_defaults().chrome)
    profile = os.environ.get("HIGHDEAS_CHROME_PROFILE", "Default")

    def launch(url):
        subprocess.Popen([chrome, f"--profile-directory={profile}", url])

    return launch


def main():
    _set_windows_app_id()
    app, service = build_app()
    _ingest_continuously(service)
    _start_upload_listener(build_upload_app(service))
    if os.environ.get("HIGHDEAS_DESKTOP", "1") == "1" and _run_desktop(app):
        return
    _run_browser(app)


def _start_upload_listener(upload_app):
    """Serve the upload app to the LAN in a daemon thread, in both desktop and
    browser modes. Only the upload route is exposed on 0.0.0.0 — the inbox UI
    with its submit/delete routes stays loopback-only.

    Failures here disable phone uploads, never the app: a typo'd port or an
    already-taken one must not kill a window the user is looking at (or,
    under the pythonw taskbar launch, exit with no console at all)."""
    if upload_app is None:
        return
    raw_port = os.environ.get("HIGHDEAS_UPLOAD_PORT", "5055")
    try:
        port = int(raw_port)
    except ValueError:
        print(f"HIGHDEAS_UPLOAD_PORT={raw_port!r} is not a port number; "
              "phone uploads are off until it's fixed.")
        return

    def serve():
        try:
            upload_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
        except Exception as exc:  # noqa: BLE001 — surface the dead listener, keep the app alive
            print(f"The upload listener could not serve on port {port} ({exc}); "
                  "phone uploads are off for this run.")

    threading.Thread(target=serve, daemon=True, name="highdeas-upload").start()



# How long the scan waits before looking in the inbox again. It is a directory listing
# — no model, no decoding, and never a recording that iCloud is still bringing down —
# so a new memo can be picked up within a few seconds of the phone dropping it in.
INGEST_INTERVAL = 5.0


def _ingest_continuously(service, *, interval=INGEST_INTERVAL, stop=None):
    """Ingest and transcribe waiting recordings, over and over, off the UI thread.

    Ingestion belongs to the app, not to whichever page is on screen: a memo recorded
    while the bin is showing must still land in the inbox, ready and transcribed by
    the time the user goes back to it. Running off the UI thread is what lets the
    window open instantly — the startup catch-up and the model load never block the
    first frame; memos stream in as they finish (the /pending poll surfaces them).

    A bad recording must never crash startup or end the scan, so swallow errors."""
    stop = stop or threading.Event()

    def run():
        while True:
            try:
                service.refresh()
            except Exception as exc:  # noqa: BLE001 — one bad recording must not end the scan
                print(f"Background transcription failed ({exc}).")
            if stop.wait(interval):
                return

    threading.Thread(target=run, daemon=True, name="highdeas-ingest").start()


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
        window = _open_window(webview, WINDOW_STATE)
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


def _open_window(webview, state_path):
    """Open the native window where it was left — same monitor, size, and maximized
    state — and keep following it so the next launch can do the same."""
    geometry = load_geometry(state_path).reachable_on(webview.screens)
    window = webview.create_window(
        APP_NAME, html=_SPLASH_HTML, background_color="#0f172a", **geometry.window_kwargs(),
    )
    WindowGeometryTracker(state_path, geometry).attach(window)
    return window


def _open_when_ready(window, url, wait_until_ready):
    """Wait for the local server, then swap the splash for the real app. The model
    load and backlog transcription happen in the background, never on this path."""
    _dress_mac_dock()
    wait_until_ready()
    window.load_url(url)


def _dress_mac_dock():
    """Put the leaf on the running app's Dock tile on macOS.

    The committed Dock-tile artwork (same squircle as the pinned launcher, so open and closed tiles match). A venv python re-execs through the Python framework's own app bundle for
    GUI work, so the running process would otherwise wear Python's rocket icon
    no matter how it was launched (the pinnable Highdeas.app launcher only
    dresses the *tile that launches it* — see tools/make_mac_app.sh). Purely
    cosmetic, so any failure is swallowed; a no-op off macOS."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage

        icon = PROJECT_ROOT / "highdeas-dock.png"
        if icon.is_file():
            # This runs on a pywebview worker thread; AppKit renders an alpha
            # image corruptly (a black tile) unless the paint happens on the
            # main thread's run loop.
            NSApplication.sharedApplication().performSelectorOnMainThread_withObject_waitUntilDone_(
                "setApplicationIconImage:", NSImage.alloc().initWithContentsOfFile_(str(icon)), False)
    except Exception:  # noqa: BLE001 — cosmetics must never break the launch
        pass


def _run_browser(app):
    port = int(os.environ.get("HIGHDEAS_PORT", "5000"))
    if os.environ.get("HIGHDEAS_OPEN_BROWSER", "1") == "1":
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

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:  # noqa: BLE001 — non-Windows or unavailable; harmless
        pass


if __name__ == "__main__":
    main()
