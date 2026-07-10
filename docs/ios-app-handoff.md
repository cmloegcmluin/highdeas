# iOS capture app — handoff

Written 2026-07-10 in a Claude Code session with Douglas on the Windows PC, as the
kickoff brief for a Claude Code session on his Mac. The decisions below were made with
him — don't relitigate them — but do settle the one open question before writing code.

## Mission

Replace the capture leg of Highdeas — an iOS Shortcut saving into iCloud Drive, mirrored
to the PC by iCloud for Windows, sometimes hours late — with a native iOS app that
records and pushes each memo straight to the Highdeas server over HTTP. The Windows PC
stays the brain: ingest, transcription, the inbox UI, and routing to Notesnook/Drive
don't move.

v1 features, agreed with Douglas:

- **Record**, continuing while the screen is locked (`UIBackgroundModes: audio`).
- **A list of recordings still on the phone**, each playable with a **scrub** slider.
- **Append** more audio to the end of an existing recording — the Voice Memos feature he
  still uses from time to time; the Shortcut can't do it.
- **Push** to the server with a retry queue. A recording is cleared from the phone only
  after the server confirms receipt — same principle as the inbox's "keep notes in the
  inbox unless the server confirms the submit."

Out of scope for v1: on-phone transcription, routing to Notesnook/Drive from the phone,
transcript editing, anything multi-user, App Store / TestFlight distribution.

## Open question — settle with Douglas before coding

Auto-push vs. append: if every recording pushes the moment it stops, there is never
anything left on the phone to append to. Manual push (per-row plus Push All)? Auto-push
after a grace window? Auto-push, with append only for the not-yet-pushed? Ask him.

## Decisions already made

- **Distribution: free Apple ID ("Personal Team") signing via Xcode.** Douglas chose the
  free weekly-re-sign route over the $99/yr Developer Program. Consequences: installs
  expire after 7 days, so make the weekly refresh one action (Run in Xcode, or script
  `xcodebuild` + `xcrun devicectl device install app`); at most 3 sideloaded apps; the
  iPhone needs Developer Mode enabled once (Settings → Privacy & Security) and the
  certificate trusted once (Settings → General → VPN & Device Management). Background
  audio is an Info.plist key, not a gated entitlement — it works under free signing, and
  nothing in this app needs the entitlements free accounts lack.
- **The app lives in this repo** under `ios/` — SwiftUI, one small Xcode project. Server
  work stays in `src/highdeas/`. One repo, so the upload contract and its client evolve
  in the same commits.
- **Audio format: AAC `.m4a`**, like the Shortcut produces today (ingest facts below).

## Orientation — what exists today

Read the README first. The pipeline: iOS Shortcut records → file lands in iCloud Drive
`VoiceInbox/` → iCloud for Windows mirrors it to the PC (the hours-late link this
project removes) → `service.refresh()` ingests and transcribes it into a local Flask
inbox → Submit routes it to Notesnook or Google Drive and retires it to a bin.

Facts the upload work must honor (`src/highdeas/ingest.py`):

- Ingest adopts any file in `VOICE_INBOX_DIR` whose suffix is in `AUDIO_EXTENSIONS`
  (`.m4a .mp3 .wav .aac .caf .aiff`). It can see a file the moment it exists, so an
  upload must never leave a partial file under an audio extension — stream to a temp
  name ingest ignores (e.g. `.part`), then rename into place.
- Recordings are keyed by content (`recording_key`): a fingerprint of size + embedded
  recording time, folded into the filename. Re-uploading the same file is therefore
  already harmless, and recycled filenames can't collide. Don't invent a parallel
  dedupe scheme.
- `recording_time` prefers the `moov/mvhd` creation time inside the m4a; iOS stamps it
  when recording. After an append/stitch, verify the exported file still carries a sane
  creation time — `AVMutableComposition` exports write a fresh container.
- While the inbox page is open it polls `GET /pending`, which calls `service.refresh()`,
  so a file dropped into the inbox dir is adopted within a poll. The upload endpoint may
  also trigger a refresh itself so adoption doesn't wait for a page to be open.

Server binding today (`src/highdeas/app.py`): desktop mode runs Flask on a **random,
loopback-only port** behind the native window; browser mode on `127.0.0.1:VOICE_PORT`
(default 5000). Nothing is reachable from the LAN yet.

## Workstream 1 — server (Python, this repo, strict TDD)

1. **`POST /upload`** on the Flask app: multipart audio file, auth via a shared token
   (`VOICE_UPLOAD_TOKEN`, new `.env` key — add it to `.env.example` and the README
   config table). Write atomically into `VOICE_INBOX_DIR` as above. Respond 2xx only
   once the file is fully in place — the phone clears a recording on 2xx and must never
   lose one. Reject a missing/bad token (401) and non-audio suffixes.
2. **A stable, LAN-reachable listener.** The phone needs a fixed `http://<pc>:<port>`
   in both desktop and browser modes. Prefer exposing **only** the upload endpoint on
   `0.0.0.0` (a second listener/port) rather than binding the whole inbox UI to the
   LAN — the UI's submit/delete routes shouldn't become LAN-wide side effects. New env
   var for the port.
3. **Windows-side notes for the README** (Douglas applies them on the PC after
   pulling): a one-time Windows Firewall inbound allowance for that port, setting
   `VOICE_UPLOAD_TOKEN` in `.env`, and finding the PC's LAN address to enter in the
   phone's settings screen. Reachability beyond the home LAN (e.g. Tailscale) was
   discussed but is not part of v1 — the retry queue covers away-from-home recording.

## Workstream 2 — the app (`ios/`)

- SwiftUI. Target the iOS version his iPhone actually runs — ask at kickoff.
- Record: `AVAudioSession` (`.playAndRecord`), `AVAudioRecorder` → AAC `.m4a` in the
  app's Documents; `UIBackgroundModes: audio` so recording survives the screen locking.
- List: local recordings with their state — recording / local / queued / sent. Play
  with a scrub slider (`AVAudioPlayer` + `Slider`).
- Append: record a new segment, stitch with `AVMutableComposition` +
  `AVAssetExportSession` (mind the creation-time fact above).
- Push: multipart POST with the token header; a `URLSession` background session with
  retry/backoff; mark sent and clear only on 2xx.
- Settings: server URL + token, plain editable fields.
- Tests: XCTest the pure logic (queue state machine, stitch bookkeeping, request
  building). The audio/hardware layer is verified on the device — don't fake-TDD it.

## Dev loop on the Mac

- Python baseline first: `python3 -m venv .venv`,
  `.venv/bin/python -m pip install -e ".[dev]"`, then `.venv/bin/python -m pytest` —
  green before touching anything. The code is cross-platform; the Windows-only bits
  (WebView2 window, taskbar identity) are guarded and fall back cleanly.
- Run the server in browser mode with temp dirs (the defaults are Windows paths):
  `VOICE_DESKTOP=0` plus `VOICE_INBOX_DIR`, `VOICE_BIN_DIR`, and `VOICE_DB` pointed at
  scratch locations, then `.venv/bin/python -m highdeas.app`. Phone and Mac on the same
  Wi-Fi gives a true end-to-end loop; production is the same code on the PC once
  Douglas pulls.
- Heads-up: opening a memo's editor autoplays its audio — recordings are private, so
  mute the Mac before driving the UI.
- The first transcription downloads the ASR model and takes ~15s; it runs in the
  background and doesn't matter to upload testing.

## Working agreements (unchanged from every Highdeas session)

- Never work in the primary checkout — `git worktree add .claude/worktrees/<name> -b
  claude/<name>`.
- Python side: strict red-green-refactor TDD; the whole suite green (zero failures,
  errors, skips) before every commit; small single-purpose commits.
- Keep `.env.example`, the README config table, and `pyproject.toml` in sync with what
  the code reads.

## Suggested order

1. Mac baseline: venv, pytest green, server runs in browser mode.
2. Settle the auto-push question with Douglas.
3. Server: `/upload` + the reachable listener (TDD).
4. Xcode skeleton in `ios/` running on his iPhone under free signing — prove the 7-day
   re-sign loop early; it's the only genuinely unfamiliar mechanic.
5. Record → push, end to end against the Mac-hosted server.
6. Scrub, then append.
7. README: the Windows-side setup notes and a short section on the iOS app.
