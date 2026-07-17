# Highdeas

Turns iPhone voice memos into finished notes and filed audio with almost no manual
work. Record on your phone; seconds later the memo is transcribed and waiting in a
local inbox page, one click from becoming a Notesnook note, a filed Google Drive
recording, or a subtask on an Asana task.

## How it works

1. **Capture** — an iOS Shortcut records audio and drops it into iCloud Drive
   (`Shortcuts/Highdeas/`), which iCloud for Windows mirrors to this PC. No Voice Memos
   app, no manual upload.
2. **Ingest** — the app watches the inbox and adopts each new recording under a
   content-unique name, so a recycled inbox filename can never collide with a past memo.
3. **Transcribe** — each recording is transcribed locally (`onnx-asr`, CPU), along with
   the second each word was spoken on. This runs in the background, so the window opens
   instantly and memos stream in as they finish. When the model makes out no words the
   note isn't left blank: a sung recording — which a speech model hears as nothing —
   reads `[singing]`, and anything else too unclear to make out reads `[unclear]`.
4. **Inbox** — a local Flask page opens in its own native window (Edge WebView2), at the
   size, monitor, and maximized state it was last closed at — maximized until you say
   otherwise. Each memo row leads with the three controls that act on it — a drag grip, a
   select checkbox, a group badge — then when it was recorded, its audio, a transcript
   preview, a chevron that moves the transcript into the Name field, a Name box, a
   three-icon destination picker (Notesnook / Drive / Asana — the lit icon is where
   Submit sends it; lighting Asana reveals a dropdown choosing which task the note lands
   under), and Submit / Delete. Drag a row by its grip to reorder the list; the row rides
   under the cursor while you move it. A live item count and a frozen title bar + column
   headers (carrying **Submit all** / **Trash all**) stay in reach as the list scrolls.
   Recordings that arrive while the page is open are polled in automatically. **Ctrl+F**
   opens an in-page find in that frozen header: type and the list narrows to the notes
   whose name or transcript holds it — reaching the whole transcript, the part the
   three-line preview clips off included, which the browser's own find can't see — and
   Esc brings the full list back. The bin carries the same find.
5. **Group** — ideas arrive in clusters. Tick a few notes and press the group button above
   the checkboxes: they fold into one memo whose transcript is a bullet per note, in inbox
   order, a named note reading `- Name: transcript`. Tick an existing group and the rest
   merge into it, keeping its name; or drag a note by its grip onto a group's badge to
   drop it in. Two groups have no obvious survivor, so ticking two disables the button. A
   badge in the third of the row's leading columns marks which rows are groups.

   A group is a memo the app makes, not a note promoted out of the pile. It stands where
   the topmost note stood, bound for the same destination, and it **plays all of their
   recordings, joined end to end** — with each note's word timings slid to where its
   recording lands, so the editor still lights up each word as the group plays. The notes
   themselves go to the bin, restorable if the merge was a mistake.

   Grouping comes undone two ways. Ctrl+Z (or the Undo button) walks back one merge at a
   time, so a note dragged into a group comes back out without dissolving what it joined —
   the group's recording is rejoined out of what it has left. Or reach for the group's
   badge: it shows the stack coming apart, and clicking it breaks the group all the way
   back into the separate notes it was folded from, taking the recording the app made with
   it. Either way each note returns with its own name, transcript, and recording, in the
   place it held.
6. **Edit** — clicking a transcript opens the note in a near-fullscreen editor, so a rough
   transcription gets fixed here rather than shipped out half-finished. The recording sits
   up top as a scrubbable waveform and starts playing; each word lights up in the text as
   it's spoken (highlighted, never selected, so your caret stays where you left it). The
   title has room to be read whole, and the body takes bulleted and numbered lists. Edits
   auto-save, and the words re-match to the text as you change it.
7. **Route on submit**
   - **Notesnook** — the transcript becomes a note via the Notesnook Inbox API, lists and
     all — so a group's bullets arrive as a real bulleted list. An unnamed memo is titled
     the way Notesnook names untitled notes (`Note <date> <time>`).
   - **Google Drive (music)** — the audio is copied into a dated
     `_YYYY_MM_DD_NOT_YET_PROCESSED_MUSIC` folder under your Drive base, renamed from the
     memo's name, with a `.docx` of the transcript alongside if there is one.
   - **Asana** — the transcript becomes a subtask of the parent task picked in the
     row's dropdown (the small set you configure via `ASANA_PARENT_TASKS`). A named
     memo carries its name as the task and its transcript as the notes; an unnamed one
     has only its transcript, so that becomes the task's name (falling back to the
     `Note <date> <time>` title when there is no transcript either). Only the text is
     sent — the audio never leaves this PC. The created task's link is kept for the bin.
8. **Retire to the bin** — on Submit, Delete, or being merged into a group, the recording
   leaves the inbox for a local bin, kept beside the inbox by default so the move stays
   inside iCloud and never triggers a per-file "move off iCloud" prompt. The inbox
   therefore only ever holds unprocessed recordings.
9. **Bin tab** (`/bin`) — lists everything retired (sent to Notesnook, Drive, or Asana,
   merged into a group, or deleted) with its audio, transcript, and date, plus **Restore**
   / **Delete** and bulk **Restore all** / **Empty bin**. **Where** names the destination
   that took the memo, and stays empty for the ones that went nowhere; the Drive icon
   opens the Drive folder (`HIGHDEAS_DRIVE_FOLDER_URL`) in your chosen Chrome profile,
   and the Asana icon opens the created task the same way. Items older than 90 days are
   purged automatically whenever the app runs.

## Launch it

**Pin it to the taskbar (recommended).** Run **`Create Highdeas Shortcut.bat`** once — it
rebuilds **`Highdeas.lnk`** in this folder and stamps it with the app's own Windows
taskbar identity (`System.AppUserModel.ID`), so the pinned button shows the Highdeas icon
and relaunches the app cleanly. Then right-click `Highdeas.lnk` → **Pin to taskbar**.

Pin the shortcut **file**, not the running window — pinning the live window captures a
generic `pythonw` icon that won't relaunch Highdeas. If one is stuck there, unpin it and
pin `Highdeas.lnk` instead.

**Staying current:** the app updates itself. Every launch fast-forwards to
`origin/main` first, and code that lands while a window sits open is pulled and
relaunched automatically once you've left the window alone for a minute. Offline
machines skip all of it quietly; a diverged checkout launches what it has.

**A new machine's `.env` needs more than paths:** copy `NOTESNOOK_INBOX_API_KEY`,
`ASANA_ACCESS_TOKEN`, `ASANA_PARENT_TASKS`, and `HIGHDEAS_UPLOAD_TOKEN` from an
existing machine's `.env` (and set a machine-appropriate `HIGHDEAS_DRIVE_BASE`),
or submits from that machine fail with auth errors while everything else hums.

**On the Mac:** run `tools/make_mac_app.sh` once — it builds `/Applications/Highdeas.app`
(leaf icon and all) pointed at this repo's venv. Open it, then right-click its Dock tile →
Options → **Keep in Dock**. Rebuild after moving the repo or changing the icon. (While
running, macOS routes GUI Python through its own framework app; Highdeas dresses the
running tile with its icon at launch.)

**Or just run it.** Double-click **`Run Highdeas.bat`**, or run
`.venv/Scripts/python -m highdeas.app`. It opens in its own window; the first memo takes
~15s while the transcription model loads — in the background, so the window still opens
right away. Set `HIGHDEAS_DESKTOP=0` to force plain-browser mode.

## Setup

1. **Create the virtualenv** and install the dependencies:

       py -m venv .venv
       .venv/Scripts/python -m pip install -e ".[dev]"

   (The app also runs straight from `src/` — `Run Highdeas.bat` just puts `src` on
   `PYTHONPATH` — so the editable install is optional.)
2. **Notesnook key** — run **`Set Notesnook Key.bat`** and paste your Inbox API key
   (Notesnook → Settings → Inbox → Enable Inbox API → create a key), or copy
   `.env.example` to `.env` and fill it in. Needed only to submit memos to Notesnook.
3. **Asana** — run **`Set Asana Token.bat`** and paste a personal access token
   (create one at <https://app.asana.com/0/my-apps> → Create new token), then list
   the tasks new notes can land under as `ASANA_PARENT_TASKS` in `.env`
   (`task_gid=Label` pairs separated by `;` — see `.env.example`; a task's gid is
   the long number in its URL). Needed only to submit memos to Asana.
4. **Paths** — if your inbox or Drive folders differ from the defaults, set
   `HIGHDEAS_INBOX_DIR` and `HIGHDEAS_DRIVE_BASE` in `.env`.

## Phone uploads (iOS app)

The iOS capture app (in `ios/`) pushes each recording straight to this PC over the
home Wi-Fi — no iCloud mirror, no hours-late sync. One-time setup on the PC:

1. **Set the shared token.** In `.env`, set `HIGHDEAS_UPLOAD_TOKEN` to any long
   random string (e.g. the output of `openssl rand -hex 32`, or in PowerShell:
   `-join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Max 256) })`). The
   upload listener stays off until this is set. Restart Highdeas afterwards.
2. **Allow the upload port through Windows Firewall** (one time, admin PowerShell):

       New-NetFirewallRule -DisplayName "Highdeas upload" -Direction Inbound `
         -Action Allow -Protocol TCP -LocalPort 5055 -Profile Private

   (Use your `HIGHDEAS_UPLOAD_PORT` if you changed it. `-Profile Private` keeps the
   rule off public networks — which means the network the PC actually uses must be
   classified **Private**. The profile lives on whichever adapter connects the PC —
   **Ethernet or Wi-Fi**: Settings → Network & Internet → Ethernet (or Wi-Fi → the
   network's properties) → Network profile type. Windows often defaults it to
   Public, and then this rule never matches. On a shared building network the
   per-unit password usually isolates your devices from the neighbors', and the
   token guards the endpoint regardless — but if uploads still can't get through
   after this, the building is likely blocking device-to-device traffic entirely,
   and a Tailscale tunnel between phone and PC is the way around it.)
3. **Find the PC's LAN address:** `ipconfig`, then the *IPv4 Address* of the active
   Wi-Fi/Ethernet adapter (e.g. `192.168.1.23`). Consider reserving that address for
   the PC in your router's DHCP settings so it doesn't drift.
4. **Point the phone at it:** in the app's settings screen enter the server URL
   `http://<that address>:5055` and the same token. The field takes **one URL per
   line** — list every machine that runs Highdeas (the Mac too, once its `.env`
   carries the same `HIGHDEAS_UPLOAD_TOKEN`). Recordings push to all of them at
   once; whichever machines are awake accept, the shared store keeps one copy,
   and a machine that is off simply misses a delivery it will receive by sync.
   Tailscale addresses (`http://<machine>.ts.net:5055`) work as lines too.

   **Prefer hostnames over raw DHCP addresses** — a numbered line rots when the
   router reassigns the machine's IP, and the phone then pushes at an empty
   address with no error anyone sees (learned the hard way: a week of "the PC
   never gets my notes" was one drifted digit). `http://<name>.local:5055` uses
   mDNS and follows the machine: on a Mac the name is `scutil --get
   LocalHostName`; on Windows it's the device name (Settings → System → About).
   Verify a line from the phone's browser first — a `Not Found` page means the
   machine answered; only if `.local` can't cross the network (some buildings
   filter mDNS) fall back to the IP, reserved in the router if possible.

Only `POST /upload` is reachable from the network — the inbox page and its
submit/delete routes stay loopback-only. Recordings made away from home simply wait
in the app's retry queue until the phone is back on the home Wi-Fi.

### The app itself

SwiftUI, in `ios/` (`Highdeas.xcodeproj`; the pure logic — queue state machine,
multipart request building — lives in the `HighdeasKit` package with its own tests,
`swift test`). One screen: a record button that keeps recording while the screen is
locked, and a list of recordings still on the phone. Each recording auto-pushes the
moment it stops and is deleted from the phone only when the server confirms custody
(any 2xx); until then it stays in the list, playable with a scrub slider. Settings
(gear) holds the server URL and token.

Building it onto the iPhone needs a Mac with Xcode, the phone in Developer Mode
(Settings → Privacy & Security), and the Apple Developer Program membership
(renews each July): open the project, plug the phone in, press Run — or run
`ios/resign.sh` for the same thing headlessly. Development installs live until the
provisioning profile expires (a year), so this is an annual (or new-phone) chore,
not a weekly one.

## Configuration

Everything but the keys for the destinations you use is optional. Set these in `.env`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `NOTESNOOK_INBOX_API_KEY` | — | Auth for posting notes to Notesnook. |
| `ASANA_ACCESS_TOKEN` | — | Personal access token for creating Asana subtasks. |
| `ASANA_PARENT_TASKS` | — | `gid=Label` pairs (`;`-separated) the Asana dropdown offers; the first is the default. |
| `HIGHDEAS_INBOX_DIR` | iCloud `Shortcuts/Highdeas` | Folder the iOS Shortcut drops recordings into. |
| `HIGHDEAS_DRIVE_BASE` | `G:\My Drive\voice memos (top level)` | Where music-routed audio is filed. |
| `HIGHDEAS_DRIVE_FOLDER_URL` | — | That folder's own Drive link (Share -> Copy link), for the bin's Drive icon to open. Empty = the icon does nothing. |
| `HIGHDEAS_BIN_DIR` | `Highdeas Bin` beside the inbox | Where retired recordings wait (recoverable for 90 days). |
| `HIGHDEAS_DB` | `memos.db` in this folder | SQLite store of memo state (single-machine mode). |
| `HIGHDEAS_STATE_DIR` | — | Set to a synced folder to keep memo state as per-memo files shared between machines; the local DB migrates across on first boot. |
| `HIGHDEAS_CHROME_EXE` / `HIGHDEAS_CHROME_PROFILE` | system Chrome / `Default` | Chrome + profile used to open Drive and Asana links. |
| `HIGHDEAS_DESKTOP` | `1` | `1` = native window, `0` = plain browser. |
| `HIGHDEAS_PORT` | `5000` | Local port in browser mode. |
| `HIGHDEAS_UPLOAD_TOKEN` | — | Shared secret the iOS capture app presents to `POST /upload`. Empty = the LAN upload listener never starts. |
| `HIGHDEAS_UPLOAD_PORT` | `5055` | LAN-reachable port serving only `/upload`, in both desktop and browser modes. |

## Tests

    .venv/Scripts/python -m pytest

## Not yet wired

- No-special-machine Highdeas (decided with Douglas 2026-07-10, deferred to its own
  session): both desks run the full app against state in a folder both machines
  sync; the iOS app pushes to a *list* of peers — whichever answers first — over
  Tailscale so it works away from home too. Building blocks and landmines, in
  dependency order: (1) shared memo state is the kernel — SQLite inside a sync
  folder corrupts under concurrent writers, so it needs a single-writer rule or
  per-memo state files; (2) multi-peer push must wait for (1), or memos scatter
  across per-machine inboxes (the upload endpoint's content-key dedupe already
  makes double-delivery harmless); (3) the phone's plain-HTTP is allowed to local
  addresses only — the Tailscale leg wants `tailscale cert` HTTPS or a targeted
  ATS exception; (4) the native window is winforms-coupled (`window_state.py`
  reads `window.native.WindowState`). Do NOT route recordings through an
  iCloud-synced folder instead — the Windows leg of iCloud sync is the
  sometimes-hours-late link this project exists to remove.
- The Mac Dock tile's launch bounce briefly flashes the untreated icon. Every
  static state is consistent (system-treated, one artwork source), but the
  bounce animation reads a pipeline a script-launched bundle can't reach. The
  real fix is a native shell: a small Swift app owning a WKWebView onto the
  same local server, running the Python engine as its child — first-class
  treatment in every animation because it genuinely is a native app. Its own
  session; obsoletes pywebview on the Mac.
- Grouping a multi-clip memo into one shared numbered doc.
- A single-file standalone `.exe`. The taskbar shortcut still launches through the
  project's `.venv` (`pythonw run_highdeas.py`), so this folder and its virtualenv need
  to stay put.
