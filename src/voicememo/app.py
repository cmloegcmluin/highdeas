"""Entrypoint: build the real review service and run the local web app."""
import os
import threading
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


def build_app():
    load_dotenv(PROJECT_ROOT / ".env")
    inbox_dir = os.environ.get("VOICE_INBOX_DIR", DEFAULT_INBOX)
    db_path = os.environ.get("VOICE_DB", str(PROJECT_ROOT / "memos.db"))
    bin_dir = os.environ.get("VOICE_BIN_DIR", str(PROJECT_ROOT / "bin"))
    notesnook = NotesnookRouter(os.environ.get("NOTESNOOK_INBOX_API_KEY", ""))
    drive = DriveMusicRouter(inbox_dir, os.environ.get("VOICE_DRIVE_BASE", DEFAULT_DRIVE_BASE))
    service = ReviewService(
        inbox_dir=inbox_dir,
        store=MemoStore(db_path),
        transcriber=Transcriber(),
        bin_dir=bin_dir,
        route=Router(notesnook=notesnook, drive=drive),
    )
    return create_app(service, inbox_dir=inbox_dir, bin_dir=bin_dir)


def main():
    app = build_app()
    if os.environ.get("VOICE_DESKTOP", "1") == "1" and _run_desktop(app):
        return
    _run_browser(app)


def _run_desktop(app):
    """Open the app in its own native window (Edge WebView2). False if unavailable."""
    try:
        import webview

        webview.create_window("Voice Memos", app, width=1360, height=900)
        webview.start()
        return True
    except Exception as exc:  # noqa: BLE001 — any failure should fall back to the browser
        print(f"Native window unavailable ({exc}); opening in the browser instead.")
        return False


def _run_browser(app):
    port = int(os.environ.get("VOICE_PORT", "5000"))
    if os.environ.get("VOICE_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    app.run(port=port)


if __name__ == "__main__":
    main()
