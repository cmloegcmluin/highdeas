# Highdeas

Turns iPhone voice memos into finished notes and filed audio with almost no manual
work. Record on your phone; seconds later the memo is transcribed and waiting in a
local inbox page, one click from becoming a Notesnook note or a filed Google Drive
recording.

## How it works

1. **Capture** — an iOS Shortcut records audio and drops it into iCloud Drive
   (`VoiceInbox/`), which iCloud for Windows mirrors to this PC. No Voice Memos app,
   no manual upload.
2. **Ingest** — the app watches the inbox and adopts each new recording under a
   content-unique name, so a recycled inbox filename can never collide with a past memo.
3. **Transcribe** — each recording is transcribed locally (`onnx-asr`, CPU), along with
   the second each word was spoken on. This runs in the background, so the window opens
   instantly and memos stream in as they finish.
4. **Inbox** — a local Flask page opens in its own native window (Edge WebView2), at the
   size, monitor, and maximized state it was last closed at — maximized until you say
   otherwise. Each memo row has its audio, a transcript preview, a chevron that moves the
   transcript into the Name field, a Name box, a Notesnook⇄Drive toggle, and Submit /
   Delete. Drag a row by its number to reorder the list, or tick a few and **Consolidate**
   them into one. Row numbers, a live item count, and a frozen title bar + column headers
   (carrying **Submit all** / **Trash all**) stay in reach as the list scrolls. Recordings
   that arrive while the page is open are polled in automatically.
5. **Edit** — clicking a transcript opens the note in a near-fullscreen editor, so a rough
   transcription gets fixed here rather than shipped out half-finished. The recording sits
   up top as a scrubbable waveform and starts playing; each word lights up in the text as
   it's spoken (highlighted, never selected, so your caret stays where you left it). The
   title has room to be read whole, and the body takes bulleted and numbered lists. Edits
   auto-save, and the words re-match to the text as you change it.
6. **Route on submit**
   - **Notesnook** — the transcript becomes a note via the Notesnook Inbox API, lists and
     all. An unnamed memo is titled the way Notesnook names untitled notes
     (`Note <date> <time>`).
   - **Google Drive (music)** — the audio is copied into a dated
     `_YYYY_MM_DD_NOT_YET_PROCESSED_MUSIC` folder under your Drive base, renamed from the
     memo's name, with a `.docx` of the transcript alongside if there is one.
7. **Retire to the bin** — on Submit or Delete the recording leaves the inbox for a
   local bin, kept beside the inbox by default so the move stays inside iCloud and never
   triggers a per-file "move off iCloud" prompt. The inbox therefore only ever holds
   unprocessed recordings.
8. **Bin tab** (`/bin`) — lists everything retired (sent to Notesnook, sent to Drive, or
   deleted) with its audio, transcript, a destination icon, and date, plus **Restore** /
   **Delete** and bulk **Restore all** / **Empty bin**. The Drive icon reopens that memo
   in Drive in your chosen Chrome profile. Items older than 90 days are purged
   automatically whenever the app runs.

## Launch it

**Pin it to the taskbar (recommended).** Run **`Create Highdeas Shortcut.bat`** once — it
rebuilds **`Highdeas.lnk`** in this folder and stamps it with the app's own Windows
taskbar identity (`System.AppUserModel.ID`), so the pinned button shows the Highdeas icon
and relaunches the app cleanly. Then right-click `Highdeas.lnk` → **Pin to taskbar**.

Pin the shortcut **file**, not the running window — pinning the live window captures a
generic `pythonw` icon that won't relaunch Highdeas. If one is stuck there, unpin it and
pin `Highdeas.lnk` instead.

**Or just run it.** Double-click **`Run Highdeas.bat`**, or run
`.venv/Scripts/python -m highdeas.app`. It opens in its own window; the first memo takes
~15s while the transcription model loads — in the background, so the window still opens
right away. Set `VOICE_DESKTOP=0` to force plain-browser mode.

## Setup

1. **Create the virtualenv** and install the dependencies:

       py -m venv .venv
       .venv/Scripts/python -m pip install -e ".[dev]"

   (The app also runs straight from `src/` — `Run Highdeas.bat` just puts `src` on
   `PYTHONPATH` — so the editable install is optional.)
2. **Notesnook key** — run **`Set Notesnook Key.bat`** and paste your Inbox API key
   (Notesnook → Settings → Inbox → Enable Inbox API → create a key), or copy
   `.env.example` to `.env` and fill it in. Needed only to submit memos to Notesnook.
3. **Paths** — if your inbox or Drive folders differ from the defaults, set
   `VOICE_INBOX_DIR` and `VOICE_DRIVE_BASE` in `.env`.

## Configuration

Everything but the Notesnook key is optional. Set these in `.env`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `NOTESNOOK_INBOX_API_KEY` | — | Auth for posting notes to Notesnook. |
| `VOICE_INBOX_DIR` | iCloud `VoiceInbox` | Folder the iOS Shortcut drops recordings into. |
| `VOICE_DRIVE_BASE` | `G:\My Drive\voice memos (top level)` | Where music-routed audio is filed. |
| `VOICE_BIN_DIR` | `VoiceBin` beside the inbox | Where retired recordings wait (recoverable for 90 days). |
| `VOICE_DB` | `memos.db` in this folder | SQLite store of memo state. |
| `VOICE_CHROME_EXE` / `VOICE_CHROME_PROFILE` | system Chrome / `Default` | Chrome + profile used to open Drive links. |
| `VOICE_DESKTOP` | `1` | `1` = native window, `0` = plain browser. |
| `VOICE_PORT` | `5000` | Local port in browser mode. |

## Tests

    .venv/Scripts/python -m pytest

## Not yet wired

- A native iOS capture app that records and pushes straight to this server instead of
  waiting on the iCloud mirror. Scoped and handed off in `docs/ios-app-handoff.md`.
- Grouping a multi-clip memo into one shared numbered doc.
- A single-file standalone `.exe`. The taskbar shortcut still launches through the
  project's `.venv` (`pythonw run_highdeas.py`), so this folder and its virtualenv need
  to stay put.
