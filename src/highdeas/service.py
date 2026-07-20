"""Application service: turn inbox recordings into memos and route submissions.

The inbox is the app's main view — the list of pending memos awaiting a Notesnook
or Drive decision; the bin holds what's been retired."""
import json
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from highdeas import audio
from highdeas.ingest import find_new_recordings, recording_key, recording_time
from highdeas.store import Memo

# Recordings the app is building are written under these names first, in the bin rather
# than the inbox: a half-written file in the inbox would be ingested as a memo. A joined
# one goes on to be named by its content; a cut one replaces the recording it came from.
_JOINING = "group"
_CUTTING = "cut"

# How long to wait out another process's grip on a recording the app has to put down.
# iCloud starts uploading a file the moment it is written, and the page has just been
# streaming it; both let go within a moment, and neither can be hurried.
_LETGO_TRIES = 6
_LETGO_WAIT = 0.15


class RecordingBusy(Exception):
    """A recording the app must put down is still held open by something else on the PC."""


class Abandoned(Exception):
    """The recording being transcribed was thrown away while the model was reading it.

    Raised out of the progress callback, which is the only moment the scan gets a word
    in between pieces — so the read stops at the next piece boundary instead of working
    through to the end of a recording nobody wants."""


@dataclass(frozen=True)
class Incoming:
    """A recording in the inbox that isn't a memo yet, as the row standing in its place
    needs it: `source` is the file to play it from, still under the name it landed with,
    and `name` is the content key it will be stored under — the name its bin has to give
    when a recording is recognised as an accident and dropped before the model reads it.

    `progress` is how much of it the model has heard, 0 until the scan reaches it: a long
    recording is a minute of nothing visibly happening, and this is the number that says
    otherwise."""
    name: str
    source: str
    progress: float = 0.0


def _no_router(memo):
    """Placeholder until the Notesnook / Drive routers are wired in."""


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _word_times(words):
    """The wire format the editor reads to highlight along with the audio."""
    return json.dumps([[word.start, word.text] for word in words], separators=(",", ":"))


def _spoken(memo):
    """A memo's word timings as [[startSeconds, word], …]."""
    return json.loads(memo.word_times) if memo.word_times else []


def _shifted(memo, offset):
    """A note's word timings as they read once its recording starts `offset` seconds in."""
    return [[round(start + offset, 3), word] for start, word in _spoken(memo)]


def _uncut(spoken, start, end, length):
    """Word timings with the seconds from `start` to `end` taken out of them.

    A word is spoken until the next one starts, so a cut that touched any part of a
    word's span takes that word with it, and everything after the cut slides back by
    the length removed. That is the same overlap the editor selects its words by, so
    the words a cut takes out of the transcript are exactly the ones it takes out of
    here."""
    removed = end - start
    kept = []
    for i, (at, word) in enumerate(spoken):
        until = spoken[i + 1][0] if i + 1 < len(spoken) else length
        if at < end and until > start:
            continue
        kept.append([round(at - removed, 3), word] if at >= end else [at, word])
    return kept


def _merges(memo):
    """The trail of merges a group has swallowed, oldest first."""
    return json.loads(memo.merges) if memo.merges else []


def _trail(steps):
    """The trail as the store holds it. An empty one is empty, not the string "[]"."""
    return json.dumps(steps) if steps else ""


def _step(memos, group=None):
    """One merge: the notes it took in, and the group as it read before it took them."""
    return {"files": [memo.audio_filename for memo in memos],
            "name": group.name if group else "",
            "transcript": group.transcript if group else ""}


def _spoken_order(memos):
    """The notes a merge takes in, ordered by when each was recorded.

    A group reads — and plays — from the first thing said to the last, whichever way
    the rows above it happen to be stacked. The inbox lists newest first, so the two
    orders are opposites, and taking the list's would hand back a consolidated note
    that runs backwards. Ingest time breaks a tie, as it does in the inbox."""
    return sorted(memos, key=lambda memo: (memo.recorded_at, memo.created_at))


def _sole_name(memos):
    """The name a fresh group takes on its own: the one name among its notes, or none.

    Distinct names, not named notes — two notes carrying the same name leave one name to
    take, and the group takes it. None, too, when several distinct names are in play,
    where the page asks which and passes the answer instead of this being reached."""
    named = {memo.name.strip() for memo in memos if memo.name.strip()}
    return next(iter(named)) if len(named) == 1 else ""


def _bullet(memo, group_name=""):
    """One consolidated note as a bullet: its name, colon, then its transcript.

    The note whose name the group itself took drops the now-redundant prefix and reads
    as its transcript alone — its name is up in the title, not repeated on every line."""
    text = memo.transcript.strip()
    name = memo.name.strip()
    if name == group_name.strip():
        name = ""
    if name and text:
        return f"- {name}: {text}"
    return f"- {name or text}"


class InboxService:
    def __init__(self, *, inbox_dir, store, transcriber, bin_dir,
                 find_new=find_new_recordings, route=_no_router, clock=_now,
                 recorded_time=recording_time, join_audio=audio.join,
                 audio_length=audio.duration, cut_audio=audio.cut, sleep=time.sleep,
                 sync_settle_scans=12):
        self._inbox_dir = inbox_dir
        self._store = store
        self._transcriber = transcriber
        self._bin_dir = bin_dir
        self._find_new = find_new
        self._route = route
        self._clock = clock
        self._recorded_time = recorded_time
        self._join_audio = join_audio
        self._audio_length = audio_length
        self._cut_audio = cut_audio
        self._sleep = sleep
        self._refresh_lock = threading.Lock()
        # How many scans an already-keyed stranger waits for its state file to
        # sync in before being adopted anyway (~a minute at the scanner's pace),
        # and the per-name count of scans waited so far.
        self._sync_settle_scans = sync_settle_scans
        self._deferred_scans = {}
        # The recording the scan is reading and how much of it it has heard. One slot
        # rather than a tally per file, because the refresh lock means one recording is
        # ever being read; and a plain tuple, swapped whole, so the poll thread reading
        # it can only ever see one scan's pair of answers, never half of two.
        self._reading = ("", 0.0)

    def refresh(self, wait=False, adopt_now=None):
        """Ingest and transcribe any waiting recordings, skipping when a refresh is
        already running. The app's own scan, the client poll, and a second browser
        tab can all land here at once; letting two scans race on the same inbox would
        transcribe a recording twice, or crash renaming a file the other just moved.
        A skipped poll loses nothing — its next poll sees whatever landed.

        `wait=True` queues behind the running scan and then scans again, for the
        one caller with no next poll: the upload endpoint fires once per landed
        recording, and the in-flight scan's snapshot predates that file.
        `adopt_now` names an upload that just landed on THIS machine, exempting
        it from the wait-for-state settle (see _should_wait_for_state)."""
        if not self._refresh_lock.acquire(blocking=wait):
            return
        try:
            self._ingest_waiting_recordings(adopt_now)
        finally:
            self._refresh_lock.release()

    def _should_wait_for_state(self, recording, adopt_now):
        """Whether to leave an already-keyed stranger un-adopted this scan.

        A recording whose filename already carries a content key but which the
        store doesn't know is usually another machine's memo whose audio synced
        in ahead of its state file. Adopting it now would write a default
        re-transcription that beats the rich memo in the sync-conflict lottery
        — the user's edits, clobbered by a blank. So it waits a few scans for
        its state to arrive. Not forever: a crash between rename and upsert on
        this machine leaves the same shape, and after the settle window it is
        still somebody's memo. Uploads land already keyed too, but their
        refresh names them via `adopt_now` — a phone push never waits."""
        keyed_already = recording.source.name == recording.name
        if not keyed_already or recording.name == adopt_now:
            return False
        waited = self._deferred_scans.get(recording.name, 0) + 1
        if waited > self._sync_settle_scans:
            self._deferred_scans.pop(recording.name, None)
            return False
        self._deferred_scans[recording.name] = waited
        return True

    def _ingest_waiting_recordings(self, adopt_now=None):
        self.purge_expired()
        # A pending memo's audio already lives in the inbox under its own name, so
        # never re-ingest it. find_new keys by content: a memo stored under a raw
        # (pre-content-key) name has a content key that differs from its filename,
        # so find_new would mistake it for a brand-new recording and re-transcribe
        # it — hanging the bin's "← Inbox" (or 500ing) and spawning a duplicate row.
        # Restore now re-keys incoming files, but a legacy raw-named memo left
        # sitting pending in the inbox never passes through restore; guard it here.
        pending = {memo.audio_filename for memo in self._store.list_pending()}
        offered = set()
        for recording in self._find_new(self._inbox_dir, self._store.known_filenames()):
            if recording.source.name in pending:
                continue
            offered.add(recording.name)
            if self._should_wait_for_state(recording, adopt_now):
                continue
            try:
                adopted = self._adopt(recording)
                spoken = self._transcriber.transcribe(adopted, progress=self._reads(recording))
                # Transcription is slow, and the store can gain a memo for this very
                # recording while it runs — the row emptied by hand (see discard), or
                # the other desk's memo syncing in. What the scan is holding is then
                # the older of the two, and writing it would clobber a settled note.
                if self._store.get(recording.name) is not None:
                    continue
                self._store.upsert(Memo(
                    audio_filename=recording.name,
                    transcript=spoken.text,
                    word_times=_word_times(spoken.words),
                    status="pending",
                    created_at=self._clock(),
                    recorded_at=self._recorded_time(adopted),
                ))
            except Abandoned:
                pass  # thrown away mid-read: it is in the bin, and there is nothing to store
            except Exception as exc:  # noqa: BLE001 — one bad recording must not strand the rest
                # A single unreadable or half-downloaded recording used to abort the whole
                # scan, hiding every recording sorted after it until that one file finally
                # decoded. Skip it and press on; the next refresh retries it (its content
                # key still isn't in the store, so nothing is lost).
                print(f"Highdeas: skipping {recording.name} this pass ({exc}).")
            finally:
                self._reading = ("", 0.0)
        # A stranger whose state file arrived stops being offered by find_new;
        # its wait-count would otherwise linger forever.
        self._deferred_scans = {name: count for name, count in self._deferred_scans.items()
                                if name in offered}

    def pending(self):
        return self._store.list_pending()

    def knows(self, audio_filename):
        """Whether this recording — pending or retired — is already in the store.
        The upload endpoint asks so a phone's retry of an already-processed
        recording is confirmed instead of landing as an orphan file."""
        return audio_filename in self._store.known_filenames()

    def _reads(self, recording):
        """The callback the transcriber tells how much of this recording it has heard.

        It is also the one moment the scan gets a word in mid-read, so it is where a
        recording thrown away in the meantime is noticed: the store holding a memo for
        it is the click having landed (see discard), and the model is stopped there
        rather than reading out the rest of a recording nobody wants."""
        def heard(done):
            if self._store.get(recording.name) is not None:
                raise Abandoned(recording.name)
            self._reading = (recording.name, done)

        self._reading = (recording.name, 0.0)
        return heard

    def incoming(self):
        """The recordings sitting in the inbox but not yet in the store, so the page can
        show them as "transcribing" the moment they land: the handoff from the phone's
        list to this one must never pass through "nowhere". A cheap directory scan — no
        model, no decoding, and nothing pulled down from iCloud — so it's safe on the
        request path."""
        reading, done = self._reading
        return [Incoming(name=recording.name, source=Path(recording.source).name,
                         progress=done if recording.name == reading else 0.0)
                for recording in self._find_new(self._inbox_dir,
                                                self._store.known_filenames())]

    def binned(self):
        """Retired memos whose recording sits in the local bin, newest first."""
        bin_path = Path(self._bin_dir)
        present = {p.name for p in bin_path.iterdir()} if bin_path.exists() else set()
        in_bin = [memo for memo in self._store.list_retired() if memo.audio_filename in present]
        return sorted(in_bin, key=lambda memo: memo.processed_at, reverse=True)

    def get(self, audio_filename):
        return self._store.get(audio_filename)

    def edit(self, audio_filename, **fields):
        self._store.update(audio_filename, **fields)

    def cut(self, audio_filename, start, end):
        """Take the seconds from `start` to `end` out of a memo's recording.

        The recording is where the transcript came from, so a stretch dragged out on the
        waveform and deleted takes the sound and the words it spoke together: the editor
        cuts the text it is showing, and this cuts what that text was read from, timings
        and all.

        The recording keeps its name. That name is a content key earned once at ingest so
        a recycled inbox filename can't collide with a past recording — not a checksum
        anything re-reads — so a cut needs no re-keying, and no row, bin entry, or undo
        step has to be re-pointed at a file that moved."""
        memo = self._store.get(audio_filename)
        recording = Path(self._inbox_dir) / audio_filename
        if memo is None or not recording.exists():
            raise ValueError("That note's recording is no longer in the inbox.")
        length = self._audio_length(recording)
        # Cut into the bin first: the inbox is scanned for new recordings, and a
        # half-written file there would be ingested as a memo of its own.
        bin_dir = Path(self._bin_dir)
        bin_dir.mkdir(parents=True, exist_ok=True)
        staged = bin_dir / f"{_CUTTING}{Path(audio_filename).suffix}"
        self._cut_audio(recording, staged, start, end)
        self._letgo(lambda: staged.replace(recording))
        self._store.update(
            audio_filename,
            word_times=json.dumps(_uncut(_spoken(memo), start, end, length),
                                  separators=(",", ":")),
            cuts=(memo.cuts or 0) + 1,
        )
        return self._store.get(audio_filename)

    def reorder(self, audio_filenames):
        """Fix the inbox to the order the user dragged its rows into."""
        self._store.reorder(audio_filenames)

    def group(self, audio_filenames, name=None):
        """Consolidate the named pending notes into a single group memo.

        A group is a memo the app makes, not a note promoted out of the pile. Every note
        picked becomes a bullet — in the order they were recorded, not the order they
        were ticked — and every one of them retires to the bin. What stands in their
        place is a new memo whose recording is theirs joined end to end, carrying their
        word timings slid to where each lands in it. It sits where the topmost note sat,
        bound for the same destination.

        The group takes a name of its own. One note named among the picks and its name
        rises to the group; several named and the page has already asked which — its
        answer arrives as `name`, a note's own or one freshly typed. A named note whose
        name the group did not take keeps it as a "- Name: transcript" prefix on its
        bullet; the one whose name rose reads as its transcript alone.

        Pick a group among them and it is the group that grows: the rest fold into its
        bullets, its recording gains theirs on the end, and its name is left alone.

        The merge joins the group's trail, so it can be walked back on its own later."""
        chosen = [m for m in self.pending() if m.audio_filename in set(audio_filenames)]
        if len(chosen) < 2:
            raise ValueError("Grouping needs at least two notes still in the inbox.")
        groups = [memo for memo in chosen if memo.kind == "group"]
        if len(groups) > 1:
            raise ValueError("Two groups have no obvious survivor; merge into one at a time.")
        group = groups[0] if groups else None
        absorbed = [memo for memo in chosen if memo is not group]
        for memo in absorbed:
            self._retire(memo.audio_filename, "grouped")
        if group is None:
            return self._found_group(chosen, name)
        spoken = _spoken_order(absorbed)
        trail = _merges(group) + [_step(spoken, group)]
        return self._rejoin(group, trail, name=group.name, transcript="\n".join(
            [group.transcript.rstrip(), *(_bullet(memo, group.name) for memo in spoken)]))

    def _found_group(self, members, name=None):
        """Make the memo that stands in the place of the notes it was folded from.

        Its name is the caller's pick when given, else the one name among the notes (or
        none when they are all nameless or several are named without a pick).

        `members` arrives in inbox order, so its first is the topmost note — the slot the
        group takes. What it says runs the other way, from the first thing spoken."""
        group_name = _sole_name(members) if name is None else name
        lead, spoken = members[0], _spoken_order(members)
        trail = [_step(spoken)]
        joined = self._join_members(trail, Path(lead.audio_filename).suffix)
        self._store.upsert(Memo(
            audio_filename=joined,
            transcript="\n".join(_bullet(memo, group_name) for memo in spoken),
            name=group_name,
            kind="group",
            route=lead.route,
            asana_parent=lead.asana_parent,
            status="pending",
            created_at=self._clock(),
            recorded_at=lead.recorded_at,
            position=lead.position,
            word_times=self._join_timings(trail),
            merges=_trail(trail),
        ))
        return self._store.get(joined)

    def unmerge(self, audio_filename):
        """Walk back the last merge this group swallowed, and only that one.

        The notes that merge took in return to the inbox with their own name, transcript,
        and recording, in the place each held, and the group reads as it did before it —
        its recording rejoined out of what is left. Walk back the merge that made the
        group and the group itself goes: it was never a note, and the notes it stood for
        are all back. Bullets typed into it since are let go: this is the merge coming
        undone, not the bullets being unpicked.

        A group folded before the trail existed has no merge to walk back, so it only
        stops being a group, keeping the bullets and the recording it is holding. The
        notes it ate stay in the bin, restorable one at a time.

        Answers with the group's filename, which changes with its recording — or "" when
        the group is gone."""
        group = self._store.get(audio_filename)
        if group is None or group.kind != "group":
            raise ValueError("Only a group can have a merge walked back.")
        trail = _merges(group)
        if not trail:
            self._store.update(audio_filename, kind="note")
            return audio_filename
        step = trail.pop()
        # The group's own recording goes first when this merge is what made it, because
        # putting it down is the one step the PC can refuse: something else may still be
        # holding it open. Refused after the notes were handed back, the group would sit
        # in the store with none of its members left in it, and the page would be told
        # its merge was untouched. Refused here, the merge really is untouched.
        if not trail:
            self._discard_recording(audio_filename)
        for filename in step["files"]:
            self._unretire(filename)
        if not trail:
            self._store.remove(audio_filename)
            return ""
        return self._rejoin(group, trail, name=step["name"],
                            transcript=step["transcript"]).audio_filename

    def ungroup(self, audio_filename):
        """Break a group all the way back into the separate notes it was folded from."""
        group = self._store.get(audio_filename)
        if group is None or group.kind != "group":
            raise ValueError("Only a group can be broken back up into notes.")
        while audio_filename:
            group = self._store.get(audio_filename)
            if group is None or group.kind != "group":
                break
            audio_filename = self.unmerge(audio_filename)

    def _rejoin(self, group, trail, *, name, transcript):
        """Rebuild the group's recording out of the members its trail now names.

        The recording is named by its content, so a group that gains or loses a member
        changes its filename with it — the memo is re-keyed onto the new recording and
        the one it replaces is dropped."""
        joined = self._join_members(trail, Path(group.audio_filename).suffix)
        if joined != group.audio_filename:
            self._store.rekey(group.audio_filename, joined)
            self._discard_recording(group.audio_filename)
        self._store.update(joined, name=name, transcript=transcript, kind="group",
                           merges=_trail(trail), word_times=self._join_timings(trail))
        return self._store.get(joined)

    def _members(self, trail):
        """Every note a group has swallowed, in the order its bullets read."""
        return [filename for step in trail for filename in step["files"]]

    def _recording(self, audio_filename):
        """Where a member's recording is right now: the inbox if it is back, else the bin."""
        landed = Path(self._inbox_dir) / audio_filename
        binned = Path(self._bin_dir) / audio_filename
        return landed if landed.exists() else (binned if binned.exists() else None)

    def _join_members(self, trail, suffix):
        """The group's recording: its members' joined, named by its own content.

        Written into the bin first. The inbox is scanned for new recordings, and a
        half-written file there would be ingested as a memo of its own."""
        sources = [path for path in map(self._recording, self._members(trail)) if path]
        bin_dir = Path(self._bin_dir)
        bin_dir.mkdir(parents=True, exist_ok=True)
        staged = bin_dir / f"{_JOINING}{suffix}"
        self._join_audio(sources, staged)
        joined = recording_key(staged)
        staged.replace(Path(self._inbox_dir) / joined)
        return joined

    def _join_timings(self, trail):
        """The members' word timings, each slid to where its recording starts."""
        spoken, offset = [], 0.0
        for filename in self._members(trail):
            source = self._recording(filename)
            if source is None:
                continue
            memo = self._store.get(filename)
            if memo is not None:
                spoken.extend(_shifted(memo, offset))
            offset += self._audio_length(source)
        return json.dumps(spoken, separators=(",", ":"))

    def _letgo(self, act):
        """Do something to a recording that something else on the PC may still be holding.

        Windows refuses to delete or replace a file another process has open, and these
        recordings have just been handed to two of them: iCloud, which starts uploading
        one the moment it is written, and the page, which has been streaming it into an
        <audio> element. Both let go within a moment and neither can be hurried, so wait
        them out. If the grip outlasts that, say so — the caller has not moved anything
        yet, and must not."""
        for attempt in range(_LETGO_TRIES):
            try:
                return act()
            except PermissionError:
                if attempt == _LETGO_TRIES - 1:
                    raise RecordingBusy(
                        "Something else on this PC still has that recording open. "
                        "Try again in a moment."
                    ) from None
                self._sleep(_LETGO_WAIT)

    def _discard_recording(self, audio_filename):
        """Drop a recording the app made and no memo plays any more."""
        made = Path(self._inbox_dir) / audio_filename
        self._letgo(lambda: made.unlink(missing_ok=True))

    def submit(self, audio_filename):
        outcome = self._route(self._store.get(audio_filename)) or {}
        self._retire(audio_filename, "processed", **outcome)

    def delete(self, audio_filename):
        self._retire(audio_filename, "deleted")

    def discard(self, audio_filename):
        """Throw away a recording that has landed but isn't a memo yet.

        Its row carries its audio from the moment it lands, so a recording left running
        by accident can be recognised and dropped without the model reading it first. It
        becomes a memo only far enough to be thrown away as one: an empty note, in the
        bin with everything else that left the inbox, restorable if the click was wrong
        — and in the store, which is what stops the next scan adopting it again."""
        landed = next((r for r in self._find_new(self._inbox_dir,
                                                 self._store.known_filenames())
                       if r.name == audio_filename), None)
        if landed is None:
            return
        adopted = self._adopt(landed)
        self._store.upsert(Memo(
            audio_filename=landed.name,
            created_at=self._clock(),
            recorded_at=self._recorded_time(adopted),
        ))
        self._retire(landed.name, "deleted")

    def restore(self, audio_filename):
        """Bring a binned recording back into the inbox as a pending memo.

        It rejoins the top of the list rather than the slot it once held, since the
        inbox it left may have been rearranged since — and the top is where the inbox
        puts everything that has just turned up.

        Realign the memo and its file with the recording's content key on the way
        in: a memo retired before content-keying is stored under its raw inbox
        name, and refresh() would then re-key the restored audio and adopt it as a
        second, brand-new pending memo — the restored item showed up twice."""
        landed = Path(self._inbox_dir) / audio_filename
        source = Path(self._bin_dir) / audio_filename
        if source.exists():
            shutil.move(str(source), str(landed))
        key = recording_key(landed) if landed.exists() else audio_filename
        if key != audio_filename:
            landed.replace(Path(self._inbox_dir) / key)
            if self._store.get(key) is None:
                self._store.rekey(audio_filename, key)
            else:
                # A pre-fix restore already spawned this recording's keyed twin;
                # drop the raw duplicate and converge onto the keyed memo.
                self._store.remove(audio_filename)
        self._store.update(key, status="pending", processed_at="", position=None)

    def purge(self, audio_filename):
        """Permanently remove a single binned recording: its audio and its record."""
        audio = Path(self._bin_dir) / audio_filename
        if audio.exists():
            audio.unlink()
        self._store.remove(audio_filename)

    def empty_bin(self):
        """Permanently remove every recording currently in the bin."""
        for memo in self.binned():
            self.purge(memo.audio_filename)

    def restore_all(self):
        """Return every binned recording to the inbox as a pending memo."""
        for memo in self.binned():
            self.restore(memo.audio_filename)

    def purge_expired(self, *, retention_days=90):
        """Forget bin items older than the retention window: delete the audio and the record."""
        cutoff = datetime.fromisoformat(self._clock()) - timedelta(days=retention_days)
        bin_dir = Path(self._bin_dir)
        for memo in self._store.list_retired():
            if memo.processed_at and datetime.fromisoformat(memo.processed_at) < cutoff:
                audio = bin_dir / memo.audio_filename
                if audio.exists():
                    audio.unlink()
                self._store.remove(memo.audio_filename)

    def _adopt(self, recording):
        """Rename a freshly-arrived recording to its content-unique name so a
        recycled inbox filename can't collide with a past recording — in the
        store now, or in the bin once it's retired. Returns its new path."""
        target = Path(self._inbox_dir) / recording.name
        source = Path(recording.source)
        if source != target:
            source.replace(target)
        return target

    def _retire(self, audio_filename, status, **fields):
        """Take a memo out of the inbox: its recording moves to the bin and it stops
        being pending. Submitting, trashing, and grouping differ only in the status
        they leave behind, which the bin reads back as where the memo went. Extra
        fields about how it left (e.g. Asana's task link) ride the same update.

        No route touches the recording in the inbox (Notesnook and Asana never see
        the file, Drive copies it), so it lands in the bin either way; guard in
        case it's somehow already gone."""
        source = Path(self._inbox_dir) / audio_filename
        if source.exists():
            bin_dir = Path(self._bin_dir)
            bin_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(bin_dir / audio_filename))
        self._store.update(audio_filename, status=status, processed_at=self._clock(), **fields)

    def _unretire(self, audio_filename):
        """Put a memo back in the inbox where it was retired from, keeping the place it
        held. It was keyed by content on the way in, so its recording needs no re-keying
        on the way back — guard only in case the recording is no longer in the bin."""
        source = Path(self._bin_dir) / audio_filename
        if source.exists():
            shutil.move(str(source), str(Path(self._inbox_dir) / audio_filename))
        self._store.update(audio_filename, status="pending", processed_at="")
