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

from highdeas.drive_link import DriveFolderLinker, parent_id_from_folder_url
from highdeas.drive_write import DriveDocFiler
from highdeas.routers import (
    AsanaRouter, ClaudeRouter, DriveMusicRouter, NotesnookRouter, Router, parse_choices,
    read_asana_tokens,
)
from highdeas.service import InboxService
from highdeas.sheet import NameCache, SheetTerms, authorized_session, fetch_names
from highdeas.store import FolderStore, MemoStore, adopt_legacy_db
from highdeas.transcribe import Transcriber
from highdeas.update import UpdateChecker
from highdeas.upload import create_upload_app
from highdeas.vocabulary import read_lexicon
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
# accepts connections, so the user never stares at a blank frame. Painted with the
# same system colors the app's pages use (app.css: color-scheme + Canvas), so the
# splash reads as the app warming up, not as a different app passing through.
_SPLASH_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><style>
  :root { color-scheme: light dark; }
  html, body { height: 100%; margin: 0; }
  body { display: flex; align-items: center; justify-content: center;
         background: Canvas; color: CanvasText;
         font-family: -apple-system, "Segoe UI", system-ui, sans-serif; }
  .box { text-align: center; }
  .name { font-size: 1.9rem; font-weight: 650; letter-spacing: .01em; }
  .sub { margin-top: .55rem; font-size: .85rem; opacity: .6; }
  .spin { width: 34px; height: 34px; margin: 1.5rem auto 0; border-radius: 50%;
          border: 3px solid color-mix(in srgb, CanvasText 25%, transparent);
          border-top-color: #22c55e;
          animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style></head>
<body><div class="box">
  <div class="name">Highdeas</div>
  <div class="spin"></div>
  <div class="sub">Loading…</div>
</div></body></html>
"""


def _system_prefers_dark():
    """Whether the OS is in dark mode, for the one paint the web engine can't
    make: the window's own background in the instant before the splash HTML
    renders. Wrong answers cost a brief flash, so any failure means False."""
    try:
        if sys.platform == "darwin":
            style = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                   capture_output=True, text=True)
            return style.stdout.strip() == "Dark"
        if sys.platform == "win32":
            import winreg

            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except Exception:  # noqa: BLE001 — cosmetics: default to light
        pass
    return False


# The vocabulary's own files, kept together wherever the lexicon lives: the sheets its
# terms are also read from, the service-account key that reads them, and the names last
# read (so a machine that boots away from the network still knows them).
LEXICON_SOURCES = "lexicon-sources.md"
GOOGLE_KEY = "google-key.json"
NAMES_CACHE = "sheet-names.json"

# The models a note opened as a chat can be opened on, when .env doesn't say. Ids are
# what claude.ai takes in a link, labels are what the row's dropdown shows. Strongest
# first: the list is read top-down and its head is the default, so the order is how
# much model you get. Models come and go faster than this app is edited, so
# HIGHDEAS_CLAUDE_MODELS replaces the list outright — it is a default, not a floor.
CLAUDE_MODELS = ("claude-fable-5=Fable 5;claude-opus-4-8=Opus 4.8;"
                 "claude-sonnet-5=Sonnet 5;claude-haiku-4-5-20251001=Haiku 4.5")


def lexicon_path():
    """The file holding the terms transcription is read against.

    Beside the state both machines share when there is any, so a name taught at one
    desk reaches the other; in this checkout when Highdeas runs alone. `.env` overrides
    both — the list can live wherever such a list is already kept."""
    override = os.environ.get("HIGHDEAS_LEXICON", "")
    if override:
        return Path(override)
    state_dir = os.environ.get("HIGHDEAS_STATE_DIR", "")
    return Path(state_dir) / "lexicon.md" if state_dir else PROJECT_ROOT / "lexicon.md"


def terms_source():
    """What transcription is corrected toward, read afresh for every recording: the
    hand-kept lexicon, and the names in every sheet listed beside it.

    All of it is files in one folder — the sheets to read are a list he edits, not
    settings — so a new source is a line, live on the next recording, on both machines.
    Built once at startup, but nothing here reaches the network yet: signing in happens
    inside the read, so a key that is missing or has been rotated costs one failed read
    (and the names last seen), never a launch."""
    sheets = SheetTerms(lexicon_path().with_name(LEXICON_SOURCES), read=_read_sheet,
                        cache=NameCache(lexicon_path().with_name(NAMES_CACHE)))
    return lambda: read_lexicon(lexicon_path()) + sheets()


def _read_sheet(spreadsheet, cells):
    """One sheet's names, read as the service account it is shared with. One key opens
    every sheet on the list — sharing the next one with the same address is the whole
    of adding it."""
    key = os.environ.get("HIGHDEAS_GOOGLE_KEY", "") or str(
        lexicon_path().with_name(GOOGLE_KEY))
    return fetch_names(authorized_session(key), spreadsheet=spreadsheet, cell_range=cells)


def build_app():
    load_dotenv(PROJECT_ROOT / ".env")
    inbox_dir = os.environ.get("HIGHDEAS_INBOX_DIR", platform_defaults().inbox)
    db_path = os.environ.get("HIGHDEAS_DB", str(PROJECT_ROOT / "memos.db"))
    bin_dir = os.environ.get("HIGHDEAS_BIN_DIR", default_bin_dir(inbox_dir))
    store = _build_store(db_path)
    drive_base = os.environ.get("HIGHDEAS_DRIVE_BASE", platform_defaults().drive_base)
    notesnook = NotesnookRouter(os.environ.get("NOTESNOOK_INBOX_API_KEY", ""))
    drive = DriveMusicRouter(inbox_dir, drive_base)
    asana_parents = parse_choices(os.environ.get("ASANA_PARENT_TASKS", ""))
    asana = AsanaRouter(
        read_asana_tokens(asana_parents, os.environ),
        default_parent=asana_parents[0][0] if asana_parents else "",
    )
    open_link = _chrome_launcher()
    # Blank reads as unset, the way every other setting here does: `.env.example` ships
    # its keys present and empty, so `.get(key, default)` would hand back "" and leave
    # an empty model list and a session opening in the home directory.
    claude_models = parse_choices(os.environ.get("HIGHDEAS_CLAUDE_MODELS") or CLAUDE_MODELS)
    claude = ClaudeRouter(
        open_browser=open_link, open_deep_link=_deep_link_launcher(),
        folder=os.environ.get("HIGHDEAS_CLAUDE_FOLDER") or str(PROJECT_ROOT),
    )
    transcriber = Transcriber(read_terms=terms_source())
    service = InboxService(
        inbox_dir=inbox_dir,
        store=store,
        transcriber=transcriber,
        bin_dir=bin_dir,
        route=Router(notesnook=notesnook, drive=drive, asana=asana, claude=claude),
    )
    drive_folder_url = os.environ.get("HIGHDEAS_DRIVE_FOLDER_URL", "")
    app = create_app(service, inbox_dir=inbox_dir, bin_dir=bin_dir,
                     open_link=open_link, asana_parents=asana_parents,
                     claude_models=claude_models,
                     drive_folder_url=drive_folder_url,
                     drive_link_for=_drive_link_resolver(drive_folder_url),
                     updates=UpdateChecker(PROJECT_ROOT),
                     rescan=lambda: _refresh_when_free(service))
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


def _deep_link_launcher():
    """Return a callable that hands a URL to whichever app the OS has registered
    for its scheme. Chrome can't be used for this: given a "claude://" link it
    tries to navigate to it, so the Code pane never opens."""
    if sys.platform == "darwin":
        return lambda url: subprocess.Popen(["open", url])
    return os.startfile


# The folder DriveDocFiler files native Google Docs into when
# HIGHDEAS_DRIVE_DOCS_FOLDER_NAME doesn't say otherwise. Never HIGHDEAS_DRIVE_BASE's
# name -- see drive_write.py's module docstring for why that has to be a folder tree
# of its own.
_DEFAULT_DRIVE_DOCS_FOLDER_NAME = "Highdeas Voice Memo Docs"


def _drive_doc_filer():
    """A callable that files a memo's transcript as a real, native Google Doc via the
    real Drive API, authenticated as Douglas's own account -- or None when that isn't
    configured (no HIGHDEAS_GOOGLE_DOCS_TOKEN_FILE, which scripts/authorize_google_docs.py
    writes). DriveMusicRouter falls back to its own local .docx write whenever this is
    None or the call resolves to nothing, so native-Doc filing stays opt-in, not
    required -- a machine that hasn't run the one-time authorization yet still files
    memos exactly as it always has."""
    token_file = os.environ.get("HIGHDEAS_GOOGLE_DOCS_TOKEN_FILE", "")
    if not token_file:
        return None
    container_name = os.environ.get("HIGHDEAS_DRIVE_DOCS_FOLDER_NAME") or _DEFAULT_DRIVE_DOCS_FOLDER_NAME
    return DriveDocFiler(token_file, container_name).file_doc


def _drive_link_resolver(folder_url):
    """A callable that resolves a memo's dated subfolder name to that subfolder's
    own Drive link, via the real Drive API — or None when that isn't configured
    (no HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE, or folder_url isn't a real Drive
    folder link to parse a parent ID out of). The bin's Drive icon falls back to
    the static top-level folder link whenever this is None or the call resolves
    to nothing, so per-memo linking stays opt-in, not required."""
    service_account_file = os.environ.get("HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE", "")
    parent_id = parent_id_from_folder_url(folder_url)
    if not service_account_file or not parent_id:
        return None
    return DriveFolderLinker(service_account_file, parent_id).link_for


def _become_current(checker=None):
    """Launch on today's code: if main moved since this checkout last pulled,
    fast-forward and re-exec before anything else loads. Offline reads as
    current; a diverged checkout launches what it has (the runtime checker
    keeps watching and the page will say so). No loop risk: the re-exec'd
    process finds itself at origin/main and sails through."""
    checker = checker or UpdateChecker(PROJECT_ROOT)
    if checker.status()["behind"] <= 0:
        return
    try:
        checker.pull()
    except RuntimeError:
        return
    checker.respawn()


def main():
    _set_windows_app_id()
    app, service = build_app()
    _ingest_continuously(service)

    def uploads_off(reason):
        """Put the dead listener on the page (and take it back off). print
        alone vanishes under the pythonw taskbar launch: a healthy-looking
        window with a silently deaf port once cost an afternoon of notes
        stuck on the phone."""
        _print_uploads_state(reason)
        if reason:
            app.config["PHONE_UPLOADS_OFF"] = reason
        else:
            app.config.pop("PHONE_UPLOADS_OFF", None)

    _start_upload_listener(build_upload_app(service), uploads_off)
    if os.environ.get("HIGHDEAS_DESKTOP", "1") == "1" and _run_desktop(app):
        return
    _run_browser(app)


def _print_uploads_state(reason):
    if reason:
        print(reason)


def _start_upload_listener(upload_app, uploads_off=_print_uploads_state,
                           retry_delay=2.0, all_clear_after=2.0):
    """Serve the upload app to the LAN in a daemon thread, in both desktop and
    browser modes. Only the upload route is exposed on 0.0.0.0 — the inbox UI
    with its submit/delete routes stays loopback-only.

    Failures here disable phone uploads, never the app: a typo'd port or an
    already-taken one must not kill a window the user is looking at. But
    they don't get to be silent either — every path that leaves this machine
    deaf to the phone announces itself through `uploads_off(reason)`, and a
    recovery withdraws the announcement with `uploads_off(None)`.

    A failed bind retries rather than giving up: the update respawn races
    the dying parent for the port, and one lost race must not leave the
    machine deaf until the next cold start."""
    if upload_app is None:
        uploads_off("Phone uploads are off: HIGHDEAS_UPLOAD_TOKEN is missing "
                    "from the .env on this machine.")
        return
    raw_port = os.environ.get("HIGHDEAS_UPLOAD_PORT", "5055")
    try:
        port = int(raw_port)
    except ValueError:
        uploads_off(f"Phone uploads are off: HIGHDEAS_UPLOAD_PORT={raw_port!r} "
                    "is not a port number.")
        return

    def serve():
        delay = retry_delay
        while True:
            # A bind failure raises within milliseconds; a server that is
            # still running when this timer fires has genuinely started.
            all_clear = threading.Timer(all_clear_after, lambda: uploads_off(None))
            all_clear.daemon = True
            all_clear.start()
            try:
                upload_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
                return
            except Exception as exc:  # noqa: BLE001 — announce the deaf port, keep the app alive
                all_clear.cancel()
                uploads_off("Phone uploads are off: the upload listener "
                            f"would not serve on port {port} ({exc}). Retrying.")
                time.sleep(delay)
                delay = min(30.0, delay * 2)

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
        APP_NAME, html=_SPLASH_HTML,
        background_color="#202020" if _system_prefers_dark() else "#ffffff",
        **geometry.window_kwargs(),
    )
    WindowGeometryTracker(state_path, geometry).attach(window)
    return window


def _open_when_ready(window, url, wait_until_ready, become_current=None,
                     enable_context_menus=None):
    """The splash is already on screen when this runs: first the launch update
    (behind "Loading…" — the user's click must answer immediately, never sit
    invisible through a git fetch), then wait for the local server, then swap
    the splash for the real app, and finally hand that page back its right-click
    menu. The model load and backlog transcription happen in the background,
    never on this path."""
    (become_current or _become_current)()
    _match_mac_dock_tile()
    wait_until_ready()
    window.load_url(url)
    (enable_context_menus or _enable_context_menus)(window)


def _enable_context_menus(window):
    """Give the desktop window back the right-click Cut/Copy/Paste menu.

    pywebview's WebView2 backend switches the default context menus off unless
    it is running in debug mode (edgechromium.py ties AreDefaultContextMenusEnabled
    to the debug flag), which leaves every text field in the app — the note
    editor's title and transcript most of all — with no menu to cut or copy from
    on a right-click. Turn that one setting back on, on the UI thread the control
    lives on, and leave the rest of debug mode (DevTools, F12, the status bar)
    off.

    Windows only: the Cocoa backend strips the menu a different way and has no
    WebView2 to reach. Best-effort — Ctrl+C/V keep working regardless, so any
    failure just prints and moves on."""
    if sys.platform != "win32":
        return
    try:
        form = window.native            # the winforms BrowserForm
        control = form.browser.webview  # the WebView2 control it hosts

        def apply():
            _turn_on_context_menus(control)

        # Event handlers run off the UI thread; a WebView2 setting must be
        # touched on the thread that created the control, so marshal onto it.
        if form.InvokeRequired:
            from System import Action

            form.Invoke(Action(apply))
        else:
            apply()
    except Exception as exc:  # noqa: BLE001 — cosmetic; keyboard cut/copy still works
        print(f"Couldn't restore the right-click menu ({exc}).")


def _turn_on_context_menus(control):
    """Switch WebView2's default context menus back on, once its core is up.

    Called before the core has finished initializing, the setting would have
    nothing to land on, so the guard lets the caller fire and forget."""
    core = control.CoreWebView2
    if core is not None:
        core.Settings.AreDefaultContextMenusEnabled = True


def _match_mac_dock_tile():
    """Make the running Dock tile identical to the pinned one on macOS.

    The pinned tile is the system's own treated rendering of the app bundle's
    icon (macOS 26 squircle and all). The running tile of a script-launched
    process never routes through that pipeline — probed exhaustively: modern
    icon format, LaunchServices re-registration, cache flushes — so instead of
    hand artwork (which can only ever *approximate* the treatment), ask the
    system to render the bundle's icon and wear exactly that. Main thread, or
    AppKit corrupts the alpha. Cosmetic: every failure is swallowed."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSWorkspace

        for bundle in ("/Applications/Highdeas.app",
                       str(Path.home() / "Applications/Highdeas.app")):
            if Path(bundle).is_dir():
                icon = NSWorkspace.sharedWorkspace().iconForFile_(bundle)
                icon.setSize_((1024, 1024))
                NSApplication.sharedApplication().performSelectorOnMainThread_withObject_waitUntilDone_(
                    "setApplicationIconImage:", icon, False)
                return
    except Exception:  # noqa: BLE001 — cosmetics must never break the launch
        pass


def _run_browser(app):
    _become_current()
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
