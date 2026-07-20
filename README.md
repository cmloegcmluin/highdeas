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
   instantly and memos stream in as they finish. The "um" and "uh" you say while
   thinking are speech, so the model writes them down; they are dropped before anything
   else reads the text, and where one of them opened a sentence the word left standing
   takes the capital it was carrying. Only a whole word of the sound counts, so
   *umbrella* and *uh-huh* survive it.

   A long recording is heard in pieces. The model's exported encoder refuses anything
   past 400 seconds outright — it fails rather than answering short — so a longer one is
   transcribed six minutes at a time and put back together, word timings and all. Each
   cut lands on the quietest moment it can reach, so a seam falls in a pause rather than
   through the middle of a word.

   The model also rarely returns nothing when it heard no speech — humming comes back as
   filler ("Mm-hmm"), noise as a confident hallucination (sometimes in another script).
   So its text is relabelled before storing: an all-humming note reads `[singing]` (a run
   of humming inside speech is bracketed where it sits), and an empty or wrong-script
   result — a memo that was nothing but hesitation among them — reads `[unclear]`.
   Anything else that reads as real speech is kept exactly as heard, lines and all.

   Names it has never heard, though, come back as whatever ordinary words sound closest
   — "Highdeas" as *high ideas*, a friend's name spelled three ways across three memos —
   and the model takes no list of words to expect (its Parakeet path has no hotword
   hook). So the transcript is read against a **lexicon** afterwards: your own terms,
   one per line, in `lexicon.md` beside the shared state — or wherever
   `HIGHDEAS_LEXICON` points. A word that near-misses one of them is swapped for it,
   and a term the model split into ordinary halves ("notes nook") is gathered back into
   the one word it was spoken as, word timings and all. Ordinary speech is left alone
   about as carefully as it can be: only a run that spells the term outright is gathered
   up (so *fun times* stays fun times), a word the term merely contains is never
   swallowed (*harmonic* stays harmonic beside *xenharmonic*), a short name has to be
   spelled outright to be heard at all (*note* is a hair away from a three-letter name),
   and a word that already spells a term is left in the case it came in. The file is
   re-read for
   every recording — a name added at nine o'clock fixes the memo recorded at five past —
   and no lexicon means nothing is corrected.

   Some of those names are already written down somewhere that keeps changing, so the
   columns of **Google Sheets** can join the lexicon too. Which sheets is itself a list
   beside it — `lexicon-sources.md`, one line each: the sheet's link, then the cells the
   names are in.

       https://docs.google.com/spreadsheets/d/1AbC_def-123/edit  C2:C
       https://docs.google.com/spreadsheets/d/1XyZ_ghi-456/edit  'People'!A2:A

   Adding the next source is adding a line — no setting, no release, no restart — and
   since the file sits in the shared folder, both machines pick it up. The read signs in
   as a service account (see Setup), never interactively, so it works on a machine
   nobody is sitting at, and one key opens every sheet on the list. Each sheet is read
   at most every ten minutes; the last names read are kept beside the lexicon, so a
   laptop that wakes up away from the network still knows them, and a sheet that can't
   be reached costs a stale list rather than a lost memo.
4. **Inbox** — a local Flask page opens in its own native window (Edge WebView2), at the
   size, monitor, and maximized state it was last closed at — maximized until you say
   otherwise. Each memo row leads with the three controls that act on it — a drag grip, a
   select checkbox, a group badge — then when it was recorded, its audio, a transcript
   preview (drawn the way the editor draws it, so a note's lists read as real bulleted
   and numbered lists here too), a chevron that moves the transcript into the Name field,
   a Name box, a three-icon destination picker (Notesnook / Drive / Asana — the lit icon
   is where Submit sends it; lighting Asana reveals a dropdown choosing which task the
   note lands under), and Submit / Delete. Drag a row by its grip to reorder the list; the
   row rides under the cursor while you move it. A live item count and a frozen title bar
   + column headers (carrying **Submit all** / **Trash all**) stay in reach as the list
   scrolls. Recordings that arrive while the page is open are polled in automatically.

   A recording that has landed but isn't transcribed yet holds the place its row will
   take, and holds it with the recording already playable and a bin beside it. That is
   what a recording left running by accident is caught by: forty minutes on the player's
   clock says what it is long before a word of it has been read, and the bin drops it —
   into the bin like anything else, restorable — without the model spending itself on it.
   If the read has already started, it is called off at the next piece boundary rather
   than working through to the end of a recording nobody wants.
   Beside the player, the place the transcript will go fills up as the model reads: a
   percentage and a bar, counting the seconds of the recording actually heard rather than
   guessing at the clock, so a long one is visibly working rather than visibly hung.

   A **find** box sits in the title bar between the count and the buttons — its magnifier
   there from the start, **Ctrl+F** just putting the cursor in it: type and the list
   narrows to the notes whose name or transcript holds it, reaching the whole transcript
   (the part the three-line preview clips off included, which the browser's own find can't
   see), and Esc brings the full list back. The bin carries the same find.
5. **Group** — ideas arrive in clusters. Tick a few notes and press the group button above
   the checkboxes: they fold into one memo whose transcript is a bullet per note, in inbox
   order. The group takes a name of its own — the one name among the notes, or, when
   several are named, whichever you pick (or type fresh) at the ask that pops up. Whichever
   name rose to the group reads plain in its bullet; a named note whose name did not rise
   keeps it, reading `- Name: transcript`. Tick an existing group and the rest merge into
   it, keeping its name; or drag a note by its grip onto a group's badge to drop it in. Two
   groups have no obvious survivor, so ticking two disables the button. A badge in the
   third of the row's leading columns marks which rows are groups.

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
   up top as a waveform and starts playing, and the text keeps pace with it: the waveform
   is yellow behind its playhead and grey in front of it, and so is the transcript — the
   words already said turn the same yellow, up to the one being said. It is their colour,
   never their selection, so your caret stays where you left it and a word both said and
   picked reads as yellow letters on blue. The waveform is
   divided into the words it spoke: a hairline where each one starts and the word itself
   printed underneath. Click a word to take its whole chunk of sound — the playhead lands
   at the top of it, so clicking plays from there — and shift-click another to reach it,
   taking the run between. What you take on the waveform is selected in the transcript
   below, and what you select in the transcript lights the chunks its words were spoken
   over (the ones it holds whole), in the same blue either way: one choice, shown in both
   places. Space plays or pauses. Delete then cuts what you've taken out of the recording
   itself and out of the transcript. It reads the other way round too: delete words from
   the text and the sound they were spoken over goes with them. Either way what plays and
   what it says stay the same note. (Only whole words deleted, and only deleted — a letter
   taken out of a word, or a word typed over, is a correction and costs the recording
   nothing.)
   The title has room to be read whole, and the body takes bulleted and numbered lists:
   each button turns its list on over what you've selected, and off again when you press it
   over that same list. Edits auto-save, and the words re-match to the text as you change
   it.
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
   - **Claude** — the note opens as a prompt nobody has sent yet, in whichever Claude the
     row's dropdown names: **Code** (the default), a Claude Code session in the desktop app
     started in `HIGHDEAS_CLAUDE_FOLDER`, or **Chat**, a new claude.ai chat in your chosen
     Chrome profile. A named memo leads with its name, then its transcript. Either way the
     composer is filled and stops — nothing reaches the model until you read it and press
     Enter. Only a chat can be opened on a chosen model (a second dropdown, from
     `HIGHDEAS_CLAUDE_MODELS`); the Code link carries no model, and no link carries an
     effort level, so both are set in Claude's own composer.
     - **Chat notes stack if you don't send them.** claude.ai keeps a single draft for
       the new-chat composer (IndexedDB, `store:chat-draft:chorus-unified-composer`) and
       `?q=` **appends** to it rather than replacing it — so a chat note you open and
       walk away from is still sitting there when the next one arrives, and the two go
       as one message. No link clears it: not an empty `q`, and not `prompt=`,
       `replace=`, `reset=`, `new=` or `clear=`, all of which claude.ai ignores. Sending
       it, or emptying the box by hand, is what clears it. A Code note has no such
       shared draft — each link fills that session's own composer and replaces whatever
       the last one left there.
8. **Retire to the bin** — on Submit, Delete, or being merged into a group, the recording
   leaves the inbox for a local bin, kept beside the inbox by default so the move stays
   inside iCloud and never triggers a per-file "move off iCloud" prompt. The inbox
   therefore only ever holds unprocessed recordings.
9. **Bin tab** (`/bin`) — lists everything retired (sent to Notesnook, Drive, or Asana,
   opened in Claude, merged into a group, or deleted) with its audio, transcript, and date, plus **Restore**
   / **Delete** and bulk **Restore all** / **Empty bin**. **Where** names the destination
   that took the memo, and stays empty for the ones that went nowhere; the Drive icon
   opens, in your chosen Chrome profile, the actual dated subfolder that memo's audio was
   filed into when a Google service account is configured
   (`HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE` — see "Google Drive per-memo folder links"
   below), falling back to the static top-level folder (`HIGHDEAS_DRIVE_FOLDER_URL`)
   when it isn't, or when that subfolder can't be resolved (not yet synced up to Drive,
   or filed before this was tracked); the Asana icon opens the created task the same way.
   Items older than 90 days are purged automatically whenever the app runs.

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
relaunched automatically once you've left the window alone for a minute. A pull that
moves `pyproject.toml` installs into that checkout's virtualenv on the way past, so a
release that adds a package doesn't land as an app quietly missing it — no pull else
pays for it, since nearly none of them touch the manifest. Offline machines skip all of
it quietly; a diverged checkout, or an install that won't run, launches what it has.

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
5. **Names from a Google Sheet** (optional) — to correct transcripts toward a column of
   names you already keep in a sheet. Once, for the first sheet:
   1. In the [Google Cloud console](https://console.cloud.google.com/), create a project
      and enable the **Google Sheets API** (APIs & Services → Enable APIs).
   2. **IAM & Admin → Service accounts → Create service account** (skip both optional
      steps — Cloud roles have no bearing on a file in Drive). Open it, then
      **Keys → Add key → Create new key → JSON**, and save the download as
      `google-key.json` beside your `lexicon.md`.

   Then, for that sheet and every one after it:
   1. **Share it** with the service account's email — the `client_email` inside that
      JSON — as **Viewer**. It is a separate account and sees nothing until you do.
   2. Add a line to `lexicon-sources.md` beside the lexicon: the sheet's link, a space,
      and the cells the names are in (`C2:C`, or `'People'!A2:A` for another tab).

   The key file is a password: keep it out of git (the `.gitignore` covers the default
   name) and off anything shared. It only ever asks Google for the read-only scope.

### Google Drive per-memo folder links (optional)

Without this, the bin's Drive icon always opens the same static top-level folder
(`HIGHDEAS_DRIVE_FOLDER_URL`) no matter which memo you click. With it, the icon opens
that memo's own dated subfolder instead. Google Drive's website only ever opens a
folder by its own Drive-assigned ID — never by name or path — so this needs a real
(if narrow) Google Cloud credential: a service account with read-only access to just
that one Drive folder.

1. Make sure `HIGHDEAS_DRIVE_FOLDER_URL` is already set in `.env` — this feature
   searches *inside* that folder for the dated subfolder to link to, so it's required
   here, not only as the plain fallback link described above. If it isn't set yet: at
   drive.google.com, open the "voice memos (top level)" folder, then **Share → Copy
   link**, and paste that as `HIGHDEAS_DRIVE_FOLDER_URL` in `.env`.
2. At <https://console.cloud.google.com>, create a project (or pick an existing one),
   then enable the **Google Drive API** for it: APIs & Services → Enable APIs and
   Services → search "Google Drive API" → **Enable**.
3. **IAM & Admin → Service Accounts → Create Service Account.** Any name works (e.g.
   `highdeas-drive-reader`). No project-level roles are needed — skip that step and
   click through to Done.
4. Open the new service account → **Keys** tab → **Add Key → Create new key → JSON**.
   This downloads a `.json` key file. Save it somewhere on this PC *outside* the
   `highdeas` folder — it's a credential, and must never be committed to git. For
   example, create `C:\Users\<you>\Highdeas Secrets\` and save it there as
   `highdeas-drive-reader.json`.
5. Set `HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE` in `.env` to that file's full path
   (e.g. `HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE=C:\Users\<you>\Highdeas Secrets\highdeas-drive-reader.json`).
6. **Share the Drive folder with the service account.** Open the service account's
   details page in Cloud Console and copy its email address (looks like
   `highdeas-drive-reader@<project-id>.iam.gserviceaccount.com`). Then at
   drive.google.com, right-click the "voice memos (top level)" folder → **Share** →
   paste that email address → **Viewer** access is enough → **Send**. Skipping this
   step is the most likely way this ends up not working: without it, Drive has
   nothing shared with the service account to search, the lookup always finds
   nothing, and the icon silently falls back to the top-level folder link.
7. Restart Highdeas.

Repeat steps 4-5 (a new key file, same service account) on any other machine that
should get per-memo links too; the sharing in step 6 only needs doing once, since
it's the Drive folder — not the machine — that's granted access.

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
| `HIGHDEAS_DRIVE_FOLDER_URL` | — | That folder's own Drive link (Share -> Copy link), for the bin's Drive icon to open. Empty = the icon does nothing. Also the folder `HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE` below searches inside — required for per-memo links too, not just the fallback. |
| `HIGHDEAS_GOOGLE_SERVICE_ACCOUNT_FILE` | — | Path to a Google Cloud service account key file, so the bin's Drive icon opens the memo's own dated subfolder instead of always the top-level folder. Empty = the icon always opens the top-level folder. See "Google Drive per-memo folder links" below. |
| `HIGHDEAS_BIN_DIR` | `Highdeas Bin` beside the inbox | Where retired recordings wait (recoverable for 90 days). |
| `HIGHDEAS_LEXICON` | `lexicon.md` beside the state dir, else in this folder | Your own names and terms, one per line, that each transcript is corrected toward. |
| `HIGHDEAS_GOOGLE_KEY` | `google-key.json` beside the lexicon | Service-account key the listed sheets are shared with. |
| `HIGHDEAS_DB` | `memos.db` in this folder | SQLite store of memo state (single-machine mode). |
| `HIGHDEAS_STATE_DIR` | — | Set to a synced folder to keep memo state as per-memo files shared between machines; the local DB migrates across on first boot. |
| `HIGHDEAS_CLAUDE_FOLDER` | this checkout | Directory a **Code** note's session starts in. Claude asks once per directory whether you trust it; when the note isn't about that project, change the directory in Claude's own UI after it opens. |
| `HIGHDEAS_CLAUDE_MODELS` | Fable 5, Opus 4.8, Sonnet 5, Haiku 4.5 | `id=Label` pairs (`;`-separated) the model dropdown offers, strongest first; the first is the default. Ids are what claude.ai takes in a link (`claude-sonnet-5`), so this list is replaced, not extended, as models come and go. |
| `HIGHDEAS_CHROME_EXE` / `HIGHDEAS_CHROME_PROFILE` | system Chrome / `Default` | Chrome + profile used to open Drive, Asana, and Claude chat links. |
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
