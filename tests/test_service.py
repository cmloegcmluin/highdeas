import json
import threading
from pathlib import Path
from unittest import mock

import pytest

from highdeas.ingest import NewRecording, recording_key
from highdeas.service import InboxService, RecordingBusy
from highdeas.store import Memo, MemoStore
from highdeas.transcribe import TimedWord, Transcript


class FakeTranscriber:
    def transcribe(self, path):
        return Transcript(f"text for {Path(path).name}")


def fake_join(sources, dest):
    """Join recordings the way ffmpeg would, if a recording were just its bytes."""
    Path(dest).write_bytes(b"".join(Path(source).read_bytes() for source in sources))


def fake_length(source):
    """A second per byte, so a joined recording's offsets are countable by hand."""
    return float(len(Path(source).read_bytes()))


def fake_cut(source, dest, start, end):
    """Cut the way ffmpeg would, at the same second-per-byte scale as fake_length."""
    sound = Path(source).read_bytes()
    Path(dest).write_bytes(sound[:int(start)] + sound[int(end):])


def service_with_fake_audio(inbox, store, bin_dir, **kwargs):
    """A service whose ffmpeg is arithmetic: joining is concatenation, cutting is
    slicing, and a recording runs a second per byte."""
    return InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
                        bin_dir=bin_dir, join_audio=fake_join, audio_length=fake_length,
                        cut_audio=fake_cut, **kwargs)


def only_group(service):
    """The one group in the inbox. Its filename is its recording's, so it isn't guessable."""
    groups = [memo for memo in service.pending() if memo.kind == "group"]
    assert len(groups) == 1
    return groups[0]


def test_refresh_adopts_new_recordings_into_pending_under_their_content_key(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "voice.m4a").write_bytes(b"A")
    (inbox / "voice-2.m4a").write_bytes(b"B")
    store = MemoStore(tmp_path / "memos.db")

    def find_new(inbox_dir, known):
        return [
            NewRecording(inbox / "voice.m4a", "voice-aaaaaaaaaaaa.m4a"),
            NewRecording(inbox / "voice-2.m4a", "voice-2-bbbbbbbbbbbb.m4a"),
        ]

    service = InboxService(
        inbox_dir=inbox,
        store=store,
        transcriber=FakeTranscriber(),
        bin_dir=tmp_path / "bin",
        find_new=find_new,
        clock=lambda: "2026-07-07T00:00",
        recorded_time=lambda path: f"recorded-{Path(path).name}",
    )

    service.refresh()

    assert {m.audio_filename for m in store.list_pending()} == {
        "voice-aaaaaaaaaaaa.m4a", "voice-2-bbbbbbbbbbbb.m4a"}
    # Each raw inbox file is renamed to its content key.
    assert (inbox / "voice-aaaaaaaaaaaa.m4a").exists()
    assert not (inbox / "voice.m4a").exists()
    # Transcription and the recording time read the adopted (renamed) file.
    memo = store.get("voice-aaaaaaaaaaaa.m4a")
    assert memo.transcript == "text for voice-aaaaaaaaaaaa.m4a"
    assert memo.recorded_at == "recorded-voice-aaaaaaaaaaaa.m4a"
    assert memo.created_at == "2026-07-07T00:00"


def test_refresh_stores_when_each_word_was_spoken_alongside_the_transcript(tmp_path):
    # The editor highlights each word as the recording plays it, so ingest keeps the
    # transcriber's word timings with the memo — as JSON, ready for the page to read.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "voice.m4a").write_bytes(b"A")
    store = MemoStore(tmp_path / "memos.db")

    class TimingTranscriber:
        def transcribe(self, path):
            return Transcript("I need a dusting.", (
                TimedWord(0.96, "I"), TimedWord(1.52, "need"),
                TimedWord(2.08, "a"), TimedWord(2.32, "dusting."),
            ))

    service = InboxService(
        inbox_dir=inbox, store=store, transcriber=TimingTranscriber(),
        bin_dir=tmp_path / "bin",
        find_new=lambda inbox_dir, known: [NewRecording(inbox / "voice.m4a", "voice-aaaaaaaaaaaa.m4a")],
        clock=lambda: "2026-07-09T00:00", recorded_time=lambda path: "2026-07-09T00:00",
    )

    service.refresh()

    memo = store.get("voice-aaaaaaaaaaaa.m4a")
    assert memo.transcript == "I need a dusting."
    assert json.loads(memo.word_times) == [[0.96, "I"], [1.52, "need"], [2.08, "a"], [2.32, "dusting."]]


def test_refresh_isolates_a_failing_recording_so_the_rest_still_ingest(tmp_path):
    # One unreadable/half-synced recording must not abort the whole scan and hide every
    # recording sorted after it — the batch keeps going and the bad one is retried later.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "bad.m4a").write_bytes(b"BAD")
    (inbox / "good.m4a").write_bytes(b"GOOD")
    store = MemoStore(tmp_path / "memos.db")

    def find_new(inbox_dir, known):
        return [
            NewRecording(inbox / "bad.m4a", "bad-aaaaaaaaaaaa.m4a"),
            NewRecording(inbox / "good.m4a", "good-bbbbbbbbbbbb.m4a"),
        ]

    class PickyTranscriber:
        def transcribe(self, path):
            if Path(path).name.startswith("bad"):
                raise RuntimeError("cannot decode a half-downloaded file")
            return Transcript("a good idea")

    service = InboxService(
        inbox_dir=inbox, store=store, transcriber=PickyTranscriber(),
        bin_dir=tmp_path / "bin", find_new=find_new,
        clock=lambda: "2026-07-08T00:00", recorded_time=lambda path: "2026-07-08T00:00",
    )
    service.refresh()

    pending = {m.audio_filename for m in store.list_pending()}
    assert "good-bbbbbbbbbbbb.m4a" in pending  # ingested despite the earlier failure
    assert "bad-aaaaaaaaaaaa.m4a" not in pending  # the bad one is skipped, not stored


def test_refresh_surfaces_a_new_recording_that_reuses_a_retired_memos_name(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    store = MemoStore(tmp_path / "memos.db")

    # voice-8.m4a was already processed; its recording sits in the bin.
    store.upsert(Memo(audio_filename="voice-8.m4a", status="processed",
                      processed_at="2026-07-07T01:00"))
    (bin_dir / "voice-8.m4a").write_bytes(b"OLD-RECORDING")

    # The Shortcut recycles the name for a genuinely different new recording.
    (inbox / "voice-8.m4a").write_bytes(b"NEW-RECORDING")

    service = InboxService(inbox_dir=inbox, store=store,
                            transcriber=FakeTranscriber(), bin_dir=bin_dir,
                            clock=lambda: "2026-07-07T02:00")
    service.refresh()

    pending = store.list_pending()
    assert len(pending) == 1
    new_name = pending[0].audio_filename
    # The new recording surfaces under its own content key, not the recycled name.
    assert new_name != "voice-8.m4a"
    assert (inbox / new_name).read_bytes() == b"NEW-RECORDING"
    # The earlier processed memo and its binned audio are left untouched.
    assert store.get("voice-8.m4a").status == "processed"
    assert (bin_dir / "voice-8.m4a").read_bytes() == b"OLD-RECORDING"


def test_reorder_rearranges_the_pending_inbox(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", status="pending", recorded_at="2026-07-07T01:00"))
    store.upsert(Memo(audio_filename="b.m4a", status="pending", recorded_at="2026-07-07T02:00"))
    service = InboxService(inbox_dir="/inbox", store=store,
                            transcriber=FakeTranscriber(), bin_dir=tmp_path / "bin")

    service.reorder(["b.m4a", "a.m4a"])

    assert [m.audio_filename for m in service.pending()] == ["b.m4a", "a.m4a"]


def test_submit_routes_then_marks_processed(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", route="drive", status="pending"))
    routed = []

    service = InboxService(
        inbox_dir="/inbox",
        store=store,
        transcriber=FakeTranscriber(),
        bin_dir=tmp_path / "bin",
        route=lambda memo: routed.append((memo.audio_filename, memo.route)),
        clock=lambda: "2026-07-07T05:00",
    )

    service.submit("a.m4a")

    assert routed == [("a.m4a", "drive")]
    memo = store.get("a.m4a")
    assert memo.status == "processed"
    assert memo.processed_at == "2026-07-07T05:00"


def test_submit_persists_fields_the_router_reports(tmp_path):
    # A route can hand back fields for the memo's record — Asana reports the created
    # task's permalink — and submit stores them with the processed update, so the bin
    # can link to where the memo went.
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", route="asana", status="pending"))

    service = InboxService(
        inbox_dir="/inbox", store=store, transcriber=FakeTranscriber(),
        bin_dir=tmp_path / "bin",
        route=lambda memo: {"asana_url": "https://app.asana.com/0/0/9/f"},
        clock=lambda: "2026-07-09T05:00",
    )
    service.submit("a.m4a")

    memo = store.get("a.m4a")
    assert memo.status == "processed"
    assert memo.asana_url == "https://app.asana.com/0/0/9/f"


def test_delete_marks_memo_deleted(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", status="pending"))

    service = InboxService(
        inbox_dir="/inbox",
        store=store,
        transcriber=FakeTranscriber(),
        bin_dir=tmp_path / "bin",
        clock=lambda: "2026-07-07T06:00",
    )

    service.delete("a.m4a")

    memo = store.get("a.m4a")
    assert memo.status == "deleted"
    assert memo.processed_at == "2026-07-07T06:00"


def test_submit_retires_inbox_audio_to_bin_when_route_leaves_it(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    (inbox / "a.m4a").write_bytes(b"AUDIO")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", route="notesnook", status="pending"))

    service = InboxService(
        inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
        bin_dir=bin_dir, route=lambda memo: None, clock=lambda: "T",
    )
    service.submit("a.m4a")

    assert not (inbox / "a.m4a").exists()
    assert (bin_dir / "a.m4a").read_bytes() == b"AUDIO"
    assert store.get("a.m4a").status == "processed"


def test_retire_skips_when_the_inbox_file_is_already_gone(tmp_path):
    # Defensive: if the recording isn't in the inbox at submit time (e.g. an
    # iCloud placeholder that never materialized), retiring is a silent no-op
    # rather than a crash — the memo is still marked processed.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", route="notesnook", status="pending"))

    service = InboxService(
        inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
        bin_dir=bin_dir, route=lambda memo: None, clock=lambda: "T",
    )
    service.submit("a.m4a")

    assert not bin_dir.exists() or list(bin_dir.iterdir()) == []
    assert store.get("a.m4a").status == "processed"


def test_delete_retires_inbox_audio_to_bin(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    (inbox / "a.m4a").write_bytes(b"AUDIO")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", status="pending"))

    InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
                  bin_dir=bin_dir, clock=lambda: "T").delete("a.m4a")

    assert not (inbox / "a.m4a").exists()
    assert (bin_dir / "a.m4a").read_bytes() == b"AUDIO"
    assert store.get("a.m4a").status == "deleted"


def test_binned_lists_processed_and_deleted_with_audio_in_bin_newest_first(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "n.m4a").write_bytes(b"N")
    (bin_dir / "d.m4a").write_bytes(b"D")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="n.m4a", status="processed", route="notesnook", processed_at="2026-07-07T02:00"))
    store.upsert(Memo(audio_filename="d.m4a", status="deleted", processed_at="2026-07-07T03:00"))
    store.upsert(Memo(audio_filename="music.m4a", status="processed", route="drive", processed_at="2026-07-07T04:00"))
    store.upsert(Memo(audio_filename="p.m4a", status="pending"))

    service = InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(), bin_dir=bin_dir)

    # music.m4a (Drive) lives in Drive not the bin, so it is excluded; pending excluded; newest first
    assert [m.audio_filename for m in service.binned()] == ["d.m4a", "n.m4a"]


def test_restore_moves_audio_back_to_inbox_and_marks_pending(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "a.m4a").write_bytes(b"A")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", status="deleted", processed_at="2026-07-07T03:00"))

    InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(), bin_dir=bin_dir).restore("a.m4a")

    pending = store.list_pending()
    assert len(pending) == 1
    memo = pending[0]
    assert memo.processed_at == ""
    assert not (bin_dir / "a.m4a").exists()  # left the bin
    assert (inbox / memo.audio_filename).read_bytes() == b"A"  # back in the inbox, playable


def test_restore_drops_a_memos_old_position_so_it_lands_at_the_top(tmp_path):
    # A memo carries the slot it was dragged into. Coming back from the bin it must
    # forget it, or it reappears buried in a since-rearranged inbox instead of on top,
    # where everything else that has just turned up waits.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "old.m4a").write_bytes(b"OLD")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="here.m4a", status="pending"))
    store.upsert(Memo(audio_filename="old.m4a", status="deleted", processed_at="2026-07-07T03:00"))
    store.reorder(["here.m4a", "old.m4a"])  # old.m4a used to trail the inbox

    InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
                  bin_dir=bin_dir).restore("old.m4a")

    # Restore re-keys the recording, so identify it by what it isn't: it leads the memo
    # that stayed, instead of reclaiming the trailing slot it was dragged into.
    pending = store.list_pending()
    assert len(pending) == 2
    assert pending[1].audio_filename == "here.m4a"


def test_restoring_a_legacy_named_memo_does_not_duplicate_it_on_the_next_refresh(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    store = MemoStore(tmp_path / "memos.db")
    # A memo retired BEFORE content-keying: stored under its raw inbox name, its
    # audio sitting in the bin under that same raw name.
    store.upsert(Memo(audio_filename="voice-9.m4a", transcript="an idea",
                      status="deleted", processed_at="2026-07-07T03:00"))
    (bin_dir / "voice-9.m4a").write_bytes(b"AUDIO-BYTES")

    service = InboxService(inbox_dir=inbox, store=store,
                            transcriber=FakeTranscriber(), bin_dir=bin_dir,
                            clock=lambda: "2026-07-07T09:00")
    service.restore("voice-9.m4a")
    service.refresh()  # the page reload that re-scans the inbox

    pending = store.list_pending()
    assert len(pending) == 1  # one memo restored, not two copies
    memo = pending[0]
    assert memo.transcript == "an idea"  # the original memo, not a fresh re-ingest
    # Its audio is on disk under the memo's stored filename, so it still plays.
    assert (inbox / memo.audio_filename).read_bytes() == b"AUDIO-BYTES"
    assert not (inbox / "voice-9.m4a").exists()  # nothing left under the raw name


def test_restoring_a_legacy_memo_whose_content_key_twin_exists_converges_them(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    store = MemoStore(tmp_path / "memos.db")
    (bin_dir / "voice-9.m4a").write_bytes(b"SAME-RECORDING")
    # The raw legacy record whose audio still sits in the bin...
    store.upsert(Memo(audio_filename="voice-9.m4a", status="processed",
                      processed_at="2026-07-07T03:00"))
    # ...and the content-keyed twin an earlier (pre-fix) restore already spawned.
    key = recording_key(bin_dir / "voice-9.m4a")
    store.upsert(Memo(audio_filename=key, transcript="the idea", status="pending"))

    service = InboxService(inbox_dir=inbox, store=store,
                            transcriber=FakeTranscriber(), bin_dir=bin_dir)
    service.restore("voice-9.m4a")

    # They converge onto the single keyed memo; the raw duplicate is dropped.
    assert store.get("voice-9.m4a") is None
    assert [m.audio_filename for m in store.list_pending()] == [key]
    assert store.get(key).transcript == "the idea"  # the kept memo, with its edits
    assert (inbox / key).read_bytes() == b"SAME-RECORDING"


def test_refresh_leaves_a_raw_named_pending_memo_already_in_the_inbox_untouched(tmp_path):
    """Refresh must not re-ingest a memo that is already pending with its audio in
    the inbox. A memo stored under a raw (pre-content-key) name is the trap: its
    content key differs from its filename, so find_new mistakes it for a brand-new
    recording and re-transcribes it — hanging the bin's '← Inbox' (or 500ing) and
    spawning a duplicate row. Restore now re-keys incoming files, but a legacy
    raw-named memo left sitting pending in the inbox never passes through restore,
    so refresh has to recognize it on its own."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="voice-12.m4a", transcript="my idea", status="pending"))
    (inbox / "voice-12.m4a").write_bytes(b"AUDIO")

    service = InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
                            bin_dir=tmp_path / "bin", clock=lambda: "2026-07-07T20:00:00")
    service.refresh()  # the "← Inbox" reload that re-scans the inbox

    # Left exactly as it was — not re-adopted, re-transcribed, or duplicated.
    pending = service.pending()
    assert [m.audio_filename for m in pending] == ["voice-12.m4a"]
    assert pending[0].transcript == "my idea"  # the original memo, not a re-ingest
    assert (inbox / "voice-12.m4a").read_bytes() == b"AUDIO"


def test_purge_permanently_removes_one_binned_recording(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "a.m4a").write_bytes(b"A")
    (bin_dir / "b.m4a").write_bytes(b"B")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", status="deleted", processed_at="2026-07-07T03:00"))
    store.upsert(Memo(audio_filename="b.m4a", status="processed", processed_at="2026-07-07T04:00"))
    service = InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(), bin_dir=bin_dir)

    service.purge("a.m4a")

    assert not (bin_dir / "a.m4a").exists()
    assert store.get("a.m4a") is None
    # The other binned item is untouched.
    assert (bin_dir / "b.m4a").exists()
    assert store.get("b.m4a") is not None


def test_empty_bin_permanently_removes_every_binned_item(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "a.m4a").write_bytes(b"A")
    (bin_dir / "b.m4a").write_bytes(b"B")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", status="deleted", processed_at="2026-07-07T03:00"))
    store.upsert(Memo(audio_filename="b.m4a", status="processed", processed_at="2026-07-07T04:00"))
    store.upsert(Memo(audio_filename="p.m4a", status="pending"))
    service = InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(), bin_dir=bin_dir)

    service.empty_bin()

    assert list(bin_dir.iterdir()) == []
    assert service.binned() == []
    assert store.get("a.m4a") is None and store.get("b.m4a") is None
    # A pending memo is not in the bin, so it is left alone.
    assert store.get("p.m4a") is not None


def test_restore_all_returns_every_binned_item_to_the_inbox(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "a.m4a").write_bytes(b"A")
    (bin_dir / "b.m4a").write_bytes(b"B")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", status="deleted", processed_at="2026-07-07T03:00"))
    store.upsert(Memo(audio_filename="b.m4a", status="processed", processed_at="2026-07-07T04:00"))
    service = InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(), bin_dir=bin_dir)

    service.restore_all()

    assert len(store.list_pending()) == 2
    assert service.binned() == []
    assert sum(1 for _ in inbox.iterdir()) == 2  # both recordings back in the inbox


def test_concurrent_refreshes_transcribe_each_recording_once(tmp_path):
    """A refresh already in flight makes a second, overlapping refresh a no-op.

    The client poll, the startup catch-up, and a second browser tab can all call
    refresh at once. Two scans racing on the same inbox would otherwise transcribe
    a recording twice — or crash renaming a file the other thread already moved."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "voice.m4a").write_bytes(b"A")
    store = MemoStore(tmp_path / "memos.db")

    inside = threading.Event()
    release = threading.Event()

    class GatedTranscriber:
        def __init__(self):
            self.calls = 0

        def transcribe(self, path):
            self.calls += 1
            inside.set()
            release.wait(timeout=2)
            return Transcript("text")

    transcriber = GatedTranscriber()
    service = InboxService(
        inbox_dir=inbox, store=store, transcriber=transcriber,
        bin_dir=tmp_path / "bin", clock=lambda: "2026-07-07T00:00",
        recorded_time=lambda path: "2026-07-07T00:00",
    )

    first = threading.Thread(target=service.refresh)
    first.start()
    assert inside.wait(timeout=2)  # the first refresh is mid-transcription, holding the guard

    service.refresh()  # an overlapping refresh must skip, not transcribe the same file again

    release.set()
    first.join(timeout=2)

    assert transcriber.calls == 1
    assert len(store.list_pending()) == 1


def test_incoming_names_each_untranscribed_recording_and_where_to_play_it(tmp_path):
    # The row a recording stands in before it is a memo now carries its audio, so the
    # page needs more than a count: the file to play it from — still under the name it
    # landed with — and the key it will answer to once it is one, which is what a click
    # on its bin has to name.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "voice.m4a").write_bytes(b"A")
    store = MemoStore(tmp_path / "memos.db")
    service = InboxService(
        inbox_dir=inbox, store=store, transcriber=FakeTranscriber(), bin_dir=tmp_path / "bin",
        find_new=lambda inbox_dir, known: [NewRecording(inbox / "voice.m4a", "voice-key.m4a")])

    assert [(r.name, r.source) for r in service.incoming()] == [("voice-key.m4a", "voice.m4a")]


def test_incoming_is_empty_when_the_inbox_is_drained(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    store = MemoStore(tmp_path / "memos.db")
    service = InboxService(inbox_dir=inbox, store=store,
                            transcriber=FakeTranscriber(), bin_dir=tmp_path / "bin")

    assert service.incoming() == []


def test_discard_bins_a_recording_before_it_is_ever_transcribed(tmp_path):
    # A recording left running by accident is recognisable from its audio alone, so its
    # row can be emptied before the model spends itself reading forty minutes of nothing.
    # It becomes a memo only far enough to be thrown away as one — in the bin, restorable,
    # and known to the store, so the next scan doesn't adopt it all over again.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "voice.m4a").write_bytes(b"ACCIDENT")
    key = recording_key(inbox / "voice.m4a")
    store = MemoStore(tmp_path / "memos.db")
    service = InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
                           bin_dir=tmp_path / "bin", clock=lambda: "2026-07-19T17:00:00")

    service.discard(key)

    assert service.incoming() == []
    assert (tmp_path / "bin" / key).read_bytes() == b"ACCIDENT"
    assert [(m.audio_filename, m.status) for m in service.binned()] == [(key, "deleted")]

    service.refresh()

    assert service.pending() == []  # and the scan leaves it thrown away


def test_a_recording_thrown_away_mid_transcription_stays_thrown_away(tmp_path):
    # The scan can already be reading a recording when its row is emptied — for a long
    # one that is the likely case, since the row is there to be judged the moment it
    # lands. Whatever the model then says belongs to a memo that has already gone to the
    # bin, so the scan must leave the store as it found it rather than writing the note
    # back into the inbox, playing a recording that is no longer in it.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "voice.m4a").write_bytes(b"ACCIDENT")
    key = recording_key(inbox / "voice.m4a")
    store = MemoStore(tmp_path / "memos.db")

    class ThrownAwayMidway:
        def transcribe(self, path):
            service.discard(key)
            return Transcript("forty minutes of nothing")

    service = InboxService(inbox_dir=inbox, store=store, transcriber=ThrownAwayMidway(),
                           bin_dir=tmp_path / "bin", clock=lambda: "2026-07-19T17:00:00")

    service.refresh()

    assert service.pending() == []
    assert [(m.audio_filename, m.status) for m in service.binned()] == [(key, "deleted")]


def test_the_scan_leaves_a_memo_that_settled_while_it_was_reading(tmp_path):
    # Transcription is slow, and the store can gain a memo for this very recording
    # while it runs: the row emptied by hand, or the other desk's memo syncing in.
    # Either way what the scan is still holding is older than what is there now, and
    # writing it would clobber a note somebody else has already settled.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "voice.m4a").write_bytes(b"A")
    key = recording_key(inbox / "voice.m4a")
    store = MemoStore(tmp_path / "memos.db")

    class SettledMidway:
        def transcribe(self, path):
            store.upsert(Memo(audio_filename=key, transcript="as somebody else settled it",
                              status="pending"))
            return Transcript("what the model heard")

    service = InboxService(inbox_dir=inbox, store=store, transcriber=SettledMidway(),
                           bin_dir=tmp_path / "bin")

    service.refresh()

    assert [m.transcript for m in service.pending()] == ["as somebody else settled it"]


def _two_notes(tmp_path, first=b"AAA", second=b"BB"):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.m4a").write_bytes(first)
    (inbox / "b.m4a").write_bytes(second)
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", transcript="one", recorded_at="2026-07-10T01:00"))
    store.upsert(Memo(audio_filename="b.m4a", transcript="two", recorded_at="2026-07-10T02:00"))
    return inbox, store


def test_group_makes_a_new_memo_whose_recording_is_its_members_joined(tmp_path):
    # A group is a memo the app makes, not a note promoted out of the pile. Every note
    # picked goes to the bin; what stands in their place plays all of their recordings.
    inbox, store = _two_notes(tmp_path)
    bin_dir = tmp_path / "bin"
    service = service_with_fake_audio(inbox, store, bin_dir, clock=lambda: "2026-07-10T09:00")

    group = service.group(["a.m4a", "b.m4a"])

    assert [m.audio_filename for m in service.pending()] == [group.audio_filename]
    assert group.audio_filename not in ("a.m4a", "b.m4a")
    assert (inbox / group.audio_filename).read_bytes() == b"AAABB"
    assert (group.kind, group.name, group.transcript) == ("group", "", "- one\n- two")
    # Both notes are in the bin, playable and restorable, neither left in the inbox.
    assert sorted(m.audio_filename for m in service.binned()) == ["a.m4a", "b.m4a"]
    assert (bin_dir / "a.m4a").read_bytes() == b"AAA"
    assert not (inbox / "a.m4a").exists() and not (inbox / "b.m4a").exists()
    assert store.get("a.m4a").processed_at == "2026-07-10T09:00"


def test_group_slides_each_members_word_timings_into_the_joined_recording(tmp_path):
    # The editor lights each word as the recording plays it. In a group the recording is
    # its members' end to end, so every word after the first note's is that much later.
    inbox, store = _two_notes(tmp_path)  # "AAA" runs 3s, "BB" runs 2s
    store.update("a.m4a", word_times='[[0.5,"one"]]')
    store.update("b.m4a", word_times='[[0.25,"two"]]')
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    group = service.group(["a.m4a", "b.m4a"])

    assert json.loads(group.word_times) == [[0.5, "one"], [3.25, "two"]]


def test_group_stands_where_the_topmost_note_stood(tmp_path):
    # The group takes the place its notes left: the same slot in the list, the same
    # destination, and the recording time of the first thing it says.
    inbox, store = _two_notes(tmp_path)
    store.update("a.m4a", route="asana", asana_parent="222")
    store.reorder(["a.m4a", "b.m4a"])
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    group = service.group(["a.m4a", "b.m4a"])

    assert group.recorded_at == "2026-07-10T01:00"
    assert (group.route, group.asana_parent) == ("asana", "222")
    assert group.position == 0
    assert group.created_at == "T"


def test_group_keeps_a_trail_of_the_merges_it_swallowed(tmp_path):
    # Each merge is walked back on its own, so each leaves its own entry: the notes it
    # took in, and the group as it read before it took them.
    inbox, store = _two_notes(tmp_path)
    (inbox / "c.m4a").write_bytes(b"C")
    store.upsert(Memo(audio_filename="c.m4a", transcript="three", recorded_at="2026-07-10T03:00"))
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    first = service.group(["a.m4a", "b.m4a"])
    grown = service.group([first.audio_filename, "c.m4a"])

    assert json.loads(grown.merges) == [
        {"files": ["a.m4a", "b.m4a"], "name": "", "transcript": ""},
        {"files": ["c.m4a"], "name": "", "transcript": "- one\n- two"},
    ]


def test_group_grows_an_existing_group_rather_than_starting_a_new_one(tmp_path):
    # Pick a group among the notes and it is the group that grows: its bullets gain the
    # rest, its recording gains theirs on the end, and its own name is left alone.
    inbox, store = _two_notes(tmp_path)
    (inbox / "c.m4a").write_bytes(b"C")
    store.upsert(Memo(audio_filename="c.m4a", transcript="three", recorded_at="2026-07-10T03:00"))
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")
    first = service.group(["a.m4a", "b.m4a"])
    service.edit(first.audio_filename, name="Song ideas")

    grown = service.group([first.audio_filename, "c.m4a"])

    assert grown.transcript == "- one\n- two\n- three"
    assert grown.name == "Song ideas"
    assert (inbox / grown.audio_filename).read_bytes() == b"AAABBC"
    # The recording it replaced is gone; only the one the group now plays is left.
    assert [p.name for p in inbox.iterdir()] == [grown.audio_filename]
    assert store.get("c.m4a").status == "grouped"


def test_group_takes_the_name_of_its_one_named_note(tmp_path):
    # A single named note among the picks hands its name up to the group, so the bullet
    # it becomes drops the now-redundant prefix and reads as its transcript alone.
    inbox, store = _two_notes(tmp_path)
    store.update("b.m4a", name="Chorus")
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    group = service.group(["a.m4a", "b.m4a"])

    assert group.name == "Chorus"
    # Both routers already turn "- " lines into real lists (see routers.py).
    assert group.transcript == "- one\n- two"


def test_group_is_left_unnamed_when_no_note_is_named(tmp_path):
    # Nothing to hand up, so the group stays untitled and every bullet is a plain line.
    inbox, store = _two_notes(tmp_path)
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    group = service.group(["a.m4a", "b.m4a"])

    assert group.name == ""
    assert group.transcript == "- one\n- two"


def test_group_takes_a_name_shared_by_several_notes_without_asking(tmp_path):
    # Several notes, but one name between them: nothing to choose, so the group takes it
    # unasked (the page never opens the namer) and every bullet carrying it drops it.
    inbox, store = _two_notes(tmp_path)
    store.update("a.m4a", name="Idea")
    store.update("b.m4a", name="Idea")
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    group = service.group(["a.m4a", "b.m4a"])

    assert group.name == "Idea"
    assert group.transcript == "- one\n- two"


def test_group_takes_the_chosen_name_when_several_notes_are_named(tmp_path):
    # Two notes named, so the page asks which name the group takes and passes the answer.
    # The chosen name rises to the group and its note's bullet drops the prefix; the other
    # named note keeps its "- Name: transcript" prefix.
    inbox, store = _two_notes(tmp_path)
    store.update("a.m4a", name="Verse")
    store.update("b.m4a", name="Chorus")
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    group = service.group(["a.m4a", "b.m4a"], name="Chorus")

    assert group.name == "Chorus"
    assert group.transcript == "- Verse: one\n- two"


def test_group_takes_a_freshly_typed_name_and_keeps_every_prefix(tmp_path):
    # The name typed at the modal belongs to none of the notes, so none of them gives it
    # up: the group wears the new name and every named note keeps its prefix.
    inbox, store = _two_notes(tmp_path)
    store.update("a.m4a", name="Verse")
    store.update("b.m4a", name="Chorus")
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    group = service.group(["a.m4a", "b.m4a"], name="Song")

    assert group.name == "Song"
    assert group.transcript == "- Verse: one\n- Chorus: two"


def test_group_stays_unnamed_when_several_are_named_and_no_name_is_chosen(tmp_path):
    # No pick to go on — a stale page, or a direct call — so the group falls back to
    # untitled with every name kept as a prefix, rather than guessing which one wins.
    inbox, store = _two_notes(tmp_path)
    store.update("a.m4a", name="Verse")
    store.update("b.m4a", name="Chorus")
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    group = service.group(["a.m4a", "b.m4a"])

    assert group.name == ""
    assert group.transcript == "- Verse: one\n- Chorus: two"


def test_group_does_not_leave_a_blank_line_when_the_group_text_ends_in_a_newline(tmp_path):
    # The group's transcript is edited by hand in the editor; leaving a trailing blank
    # line is routine, and must not push an empty line between the bullets on the next
    # merge — a blank line closes the list, splitting one list into two.
    inbox, store = _two_notes(tmp_path)
    (inbox / "c.m4a").write_bytes(b"C")
    store.upsert(Memo(audio_filename="c.m4a", transcript="three", recorded_at="2026-07-10T03:00"))
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")
    first = service.group(["a.m4a", "b.m4a"])
    service.edit(first.audio_filename, transcript="- one\n- two\n")

    grown = service.group([first.audio_filename, "c.m4a"])

    assert grown.transcript == "- one\n- two\n- three"


def test_group_refuses_a_selection_holding_two_groups(tmp_path):
    # Two groups have no obvious survivor — which one's name and recording win? — so the
    # UI disables the button and the service refuses, leaving both untouched.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="g1.m4a", kind="group", transcript="- one",
                      recorded_at="2026-07-08T01:00"))
    store.upsert(Memo(audio_filename="g2.m4a", kind="group", transcript="- two",
                      recorded_at="2026-07-08T02:00"))
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    with pytest.raises(ValueError):
        service.group(["g1.m4a", "g2.m4a"])

    assert [m.audio_filename for m in service.pending()] == ["g2.m4a", "g1.m4a"]
    assert store.get("g1.m4a").transcript == "- one"


def test_group_refuses_fewer_than_two_pending_notes(tmp_path):
    # A stale selection — a row already submitted from another window — must not
    # silently turn its one surviving companion into a one-bullet "group".
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", transcript="one"))
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    with pytest.raises(ValueError):
        service.group(["a.m4a", "gone.m4a"])

    assert store.get("a.m4a").kind == "note"


def test_cut_takes_the_span_out_of_the_recording_and_out_of_its_word_timings(tmp_path):
    # The recording is where the transcript came from, so cutting a stretch of sound cuts
    # the words it spoke. A word is spoken until the next one starts, so any word whose
    # span the cut touched goes with it, and what was left slides back by what was removed.
    inbox, store = _two_notes(tmp_path, first=b"ABCDE")  # five bytes, five seconds
    store.update("a.m4a", word_times='[[0.0,"one"],[1.0,"two"],[2.0,"three"],[4.0,"four"]]')
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    memo = service.cut("a.m4a", 1.0, 3.0)

    assert (inbox / "a.m4a").read_bytes() == b"ADE"
    assert json.loads(memo.word_times) == [[0.0, "one"], [2.0, "four"]]


def test_cut_counts_itself_so_the_recording_can_be_asked_for_by_a_name_it_has_not_played(tmp_path):
    # A cut recording keeps its filename, and a player handed a URL it is already holding
    # goes on playing what it has — the browser keeps one media resource per URL, not per
    # file, and a page told not to store the response reused it anyway. So the count rides
    # with the memo, and every render of the row asks for the recording as it now is.
    inbox, store = _two_notes(tmp_path, first=b"ABCDE")
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    assert store.get("a.m4a").cuts == 0
    assert service.cut("a.m4a", 1.0, 2.0).cuts == 1
    assert service.cut("a.m4a", 0.0, 1.0).cuts == 2


def test_cut_leaves_the_memo_whole_when_the_pc_will_not_let_go_of_the_recording(tmp_path):
    # The page has been streaming this very recording, so the moment the cut has to put
    # it down is the moment Windows is most likely to refuse. Timings written before the
    # sound was replaced would leave a memo whose words no longer describe what it plays.
    inbox, store = _two_notes(tmp_path, first=b"ABCDE")
    store.update("a.m4a", word_times='[[0.0,"one"],[4.0,"four"]]')
    service = service_with_fake_audio(inbox, store, tmp_path / "bin",
                                      clock=lambda: "T", sleep=lambda seconds: None)

    with mock.patch.object(Path, "replace", side_effect=PermissionError("[WinError 32] in use")):
        with pytest.raises(RecordingBusy):
            service.cut("a.m4a", 1.0, 3.0)

    assert (inbox / "a.m4a").read_bytes() == b"ABCDE"
    assert json.loads(store.get("a.m4a").word_times) == [[0.0, "one"], [4.0, "four"]]


def test_unmerge_walks_back_the_last_merge_and_leaves_the_ones_before_it(tmp_path):
    # Undo has to step back one merge at a time. Walking back the note dragged into a
    # group must not dissolve the group the merges before it built — and the group's
    # recording is rejoined out of what is left.
    inbox, store = _two_notes(tmp_path)
    (inbox / "c.m4a").write_bytes(b"C")
    store.upsert(Memo(audio_filename="c.m4a", transcript="three", recorded_at="2026-07-10T03:00"))
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")
    first = service.group(["a.m4a", "b.m4a"])
    grown = service.group([first.audio_filename, "c.m4a"])

    left = service.unmerge(grown.audio_filename)

    group = only_group(service)
    assert group.audio_filename == left
    assert group.transcript == "- one\n- two"
    assert (inbox / left).read_bytes() == b"AAABB"
    # c was recorded last, so the inbox lists it above the group it just left.
    assert [m.transcript for m in service.pending()] == ["three", "- one\n- two"]
    assert (inbox / "c.m4a").read_bytes() == b"C"


def test_unmerge_of_the_merge_that_made_the_group_takes_the_group_with_it(tmp_path):
    # The group was never a note. Walk back the merge that made it and the notes it stood
    # for are all back, so the memo — and the recording the app made for it — go.
    inbox, store = _two_notes(tmp_path)
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")
    group = service.group(["a.m4a", "b.m4a"])

    assert service.unmerge(group.audio_filename) == ""

    assert store.get(group.audio_filename) is None
    assert not (inbox / group.audio_filename).exists()
    assert [m.audio_filename for m in service.pending()] == ["b.m4a", "a.m4a"]
    assert (inbox / "a.m4a").read_bytes() == b"AAA"
    assert service.binned() == []


def test_unmerge_leaves_the_merge_whole_when_the_pc_will_not_let_go_of_the_recording(tmp_path):
    # The group's recording is a file the app made, and something else on the PC — iCloud
    # uploading it, the page that was just streaming it — can hold it shut for a moment.
    # Deleting it came last, so the notes were already back in the inbox when it failed:
    # the group survived in the store with its members already out of it, and the page,
    # told the merge was untouched, was lied to. Nothing moves until the recording does.
    inbox, store = _two_notes(tmp_path)
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T",
                               sleep=lambda seconds: None)
    group = service.group(["a.m4a", "b.m4a"])

    with mock.patch.object(Path, "unlink", side_effect=PermissionError("[WinError 32] in use")):
        with pytest.raises(RecordingBusy):
            service.unmerge(group.audio_filename)

    assert store.get(group.audio_filename).kind == "group"
    assert [m.audio_filename for m in service.pending()] == [group.audio_filename]
    assert sorted(m.audio_filename for m in service.binned()) == ["a.m4a", "b.m4a"]
    assert (inbox / group.audio_filename).exists()


def test_unmerge_waits_out_a_recording_that_is_only_briefly_held(tmp_path):
    # A sync engine's grip on a file it has just been handed lasts a moment, not forever.
    waits = []
    inbox, store = _two_notes(tmp_path)
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T",
                               sleep=waits.append)
    group = service.group(["a.m4a", "b.m4a"])
    real_unlink, refusals = Path.unlink, [PermissionError("[WinError 32] in use")] * 2

    def grudging(self, **kwargs):
        if refusals:
            raise refusals.pop()
        return real_unlink(self, **kwargs)

    with mock.patch.object(Path, "unlink", grudging):
        assert service.unmerge(group.audio_filename) == ""

    assert waits and not refusals  # it waited, and then the file let go
    assert store.get(group.audio_filename) is None
    assert [m.audio_filename for m in service.pending()] == ["b.m4a", "a.m4a"]


def test_unmerge_refuses_a_memo_that_is_not_a_group(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", transcript="one"))
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    with pytest.raises(ValueError):
        service.unmerge("a.m4a")

    assert store.get("a.m4a").transcript == "one"


def test_ungroup_breaks_a_group_back_into_its_separate_notes(tmp_path):
    inbox, store = _two_notes(tmp_path)
    store.update("a.m4a", name="Verse")
    store.update("b.m4a", name="Chorus")
    bin_dir = tmp_path / "bin"
    service = service_with_fake_audio(inbox, store, bin_dir, clock=lambda: "T")
    group = service.group(["a.m4a", "b.m4a"])

    service.ungroup(group.audio_filename)

    # Both notes are back in the inbox, exactly as they read before the merge.
    assert [m.audio_filename for m in service.pending()] == ["b.m4a", "a.m4a"]
    lead = store.get("a.m4a")
    assert (lead.kind, lead.name, lead.transcript, lead.status) == ("note", "Verse", "one", "pending")
    assert (inbox / "b.m4a").read_bytes() == b"BB"
    assert service.binned() == []
    # The recording the app made for the group is gone with it.
    assert store.get(group.audio_filename) is None
    assert not (inbox / group.audio_filename).exists()


def test_ungroup_returns_every_note_dragged_into_an_existing_group(tmp_path):
    # A group grows one note at a time, and breaking it up hands every one of them back.
    inbox, store = _two_notes(tmp_path)
    (inbox / "c.m4a").write_bytes(b"C")
    store.upsert(Memo(audio_filename="c.m4a", transcript="three", recorded_at="2026-07-10T03:00"))
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")
    first = service.group(["a.m4a", "b.m4a"])
    grown = service.group([first.audio_filename, "c.m4a"])

    service.ungroup(grown.audio_filename)

    # Handed back to an inbox that lists newest first, so they read the other way from
    # the bullets they were — which run from the first thing said to the last.
    assert [m.audio_filename for m in service.pending()] == ["c.m4a", "b.m4a", "a.m4a"]
    assert [m.transcript for m in service.pending()] == ["three", "two", "one"]
    assert sorted(p.name for p in inbox.iterdir()) == ["a.m4a", "b.m4a", "c.m4a"]


def test_ungroup_keeps_the_text_of_a_group_that_kept_no_trail(tmp_path):
    # A group folded before the trail existed has no merge to walk back. Breaking it up
    # only stops it being a group, leaving its bullets and its recording where they are —
    # an absent trail must not be read as an empty name and an empty transcript. Its
    # notes stay in the bin, restorable one at a time.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "b.m4a").write_bytes(b"B")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="g.m4a", kind="group", name="Song ideas",
                      transcript="- one\n- two", recorded_at="2026-07-10T01:00"))
    store.upsert(Memo(audio_filename="b.m4a", transcript="two", status="grouped",
                      processed_at="2026-07-10T05:00", recorded_at="2026-07-10T02:00"))
    service = service_with_fake_audio(inbox, store, bin_dir, clock=lambda: "T")

    service.ungroup("g.m4a")

    survivor = store.get("g.m4a")
    assert (survivor.kind, survivor.name, survivor.transcript) == ("note", "Song ideas", "- one\n- two")
    assert [m.audio_filename for m in service.binned()] == ["b.m4a"]


def test_ungroup_refuses_a_memo_that_is_not_a_group(tmp_path):
    # Only a group's badge offers the break-up click, but a stale page must not strip a
    # plain note of the name and transcript it never folded away.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", name="Verse", transcript="one"))
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")

    with pytest.raises(ValueError):
        service.ungroup("a.m4a")

    assert store.get("a.m4a").transcript == "one"


def test_a_note_restored_from_the_bin_is_not_claimed_again_when_its_group_breaks_up(tmp_path):
    # Restore is the other way back out of a group. A note that has already walked it must
    # come back once, not twice, when the group it left is broken up behind it.
    inbox, store = _two_notes(tmp_path)
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "T")
    group = service.group(["a.m4a", "b.m4a"])
    service.restore("b.m4a")  # restore re-keys it by content on the way back in

    service.ungroup(group.audio_filename)

    assert sorted(m.transcript for m in service.pending()) == ["one", "two"]
    assert service.binned() == []


def test_binned_lists_the_notes_absorbed_into_a_group(tmp_path):
    # Grouping moves every picked recording into the bin, so the bin has to show them.
    # Otherwise their audio sits there unreachable — unplayable, unrestorable, and never
    # swept up by the retention purge.
    inbox, store = _two_notes(tmp_path)
    service = service_with_fake_audio(inbox, store, tmp_path / "bin", clock=lambda: "2026-07-08T09:00")

    service.group(["a.m4a", "b.m4a"])

    assert sorted(m.audio_filename for m in service.binned()) == ["a.m4a", "b.m4a"]


def test_purge_expired_removes_only_bin_items_past_retention(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "old.m4a").write_bytes(b"OLD")
    (bin_dir / "new.m4a").write_bytes(b"NEW")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="old.m4a", status="processed", processed_at="2026-04-01T00:00:00"))
    store.upsert(Memo(audio_filename="new.m4a", status="deleted", processed_at="2026-06-30T00:00:00"))

    service = InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
                            bin_dir=bin_dir, clock=lambda: "2026-07-07T00:00:00")
    service.purge_expired(retention_days=90)

    # cutoff is 2026-04-08; "old" is before it -> audio and record gone
    assert not (bin_dir / "old.m4a").exists()
    assert store.get("old.m4a") is None
    # "new" is within retention -> untouched
    assert (bin_dir / "new.m4a").exists()
    assert store.get("new.m4a") is not None


def test_refresh_purges_expired_bin_items(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "old.m4a").write_bytes(b"OLD")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="old.m4a", status="processed", processed_at="2026-01-01T00:00:00"))

    service = InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
                            bin_dir=bin_dir, find_new=lambda inbox, known: [],
                            clock=lambda: "2026-07-07T00:00:00")
    service.refresh()

    assert store.get("old.m4a") is None
    assert not (bin_dir / "old.m4a").exists()


def test_knows_covers_pending_and_retired_memos_but_not_strangers(tmp_path):
    # The upload endpoint asks before accepting a retry: a recording the store
    # already holds — still pending, or long since processed into the bin — is
    # confirmed rather than re-adopted as an orphan file in the inbox.
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="pending.m4a", status="pending"))
    store.upsert(Memo(audio_filename="retired.m4a", status="processed"))
    service = InboxService(
        inbox_dir=tmp_path / "inbox", store=store, transcriber=FakeTranscriber(),
        bin_dir=tmp_path / "bin",
    )

    assert service.knows("pending.m4a")
    assert service.knows("retired.m4a")
    assert not service.knows("new.m4a")


def test_refresh_can_wait_for_the_running_scan_instead_of_skipping(tmp_path):
    # The upload endpoint fires a refresh per landed recording. A burst of
    # pushes overlaps: the in-flight scan snapshotted the inbox before the
    # later files landed, so a skipped (non-blocking) trigger would strand
    # them until some future poll. wait=True queues the trigger instead.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.m4a").write_bytes(b"A")
    store = MemoStore(tmp_path / "memos.db")
    scanning = threading.Event()
    release = threading.Event()

    class SlowTranscriber:
        def transcribe(self, path):
            scanning.set()
            release.wait(timeout=5)
            return Transcript(f"text for {Path(path).name}")

    service = InboxService(
        inbox_dir=inbox, store=store, transcriber=SlowTranscriber(),
        bin_dir=tmp_path / "bin",
        find_new=lambda d, known: [NewRecording(p, f"{p.stem}-cccccccccccc.m4a")
                                   for p in sorted(Path(d).glob("*.m4a"))
                                   if f"{p.stem}-cccccccccccc.m4a" not in known],
        clock=lambda: "2026-07-10T00:00",
        recorded_time=lambda path: "2026-07-10T00:00",
    )

    first = threading.Thread(target=service.refresh)
    first.start()
    assert scanning.wait(timeout=5)
    (inbox / "b.m4a").write_bytes(b"B")  # lands mid-scan, after the snapshot
    waiter = threading.Thread(target=lambda: service.refresh(wait=True))
    waiter.start()
    release.set()
    first.join(timeout=5)
    waiter.join(timeout=5)

    assert {m.audio_filename for m in store.list_pending()} == {
        "a-cccccccccccc.m4a", "b-cccccccccccc.m4a"}


def _keyed_service(tmp_path, store, *, settle=2):
    """A service whose inbox offers one already-keyed recording — the shape a
    peer machine's audio has when it syncs in ahead of its state file."""
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    keyed = inbox / "voice-8-abcdefabcdef.m4a"
    keyed.write_bytes(b"AUDIO")
    return InboxService(
        inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
        bin_dir=tmp_path / "bin",
        find_new=lambda d, known: (
            [NewRecording(keyed, keyed.name)] if keyed.name not in known else []),
        clock=lambda: "2026-07-11T00:00", recorded_time=lambda path: "2026-07-11T00:00",
        sync_settle_scans=settle,
    )


def test_an_already_keyed_stranger_waits_for_its_state_to_sync_in(tmp_path):
    # Another machine's memo: its audio synced here first. Adopting it now
    # would write a default re-transcription that wins the sync conflict over
    # the rich memo about to arrive — the user's edits, clobbered.
    store = MemoStore(tmp_path / "memos.db")
    service = _keyed_service(tmp_path, store, settle=2)

    service.refresh()
    service.refresh()

    assert store.known_filenames() == set()


def test_the_wait_ends_if_no_state_ever_comes(tmp_path):
    # The same shape is left by a crash between rename and upsert on THIS
    # machine — after the settle window it must still become a memo.
    store = MemoStore(tmp_path / "memos.db")
    service = _keyed_service(tmp_path, store, settle=2)

    service.refresh()
    service.refresh()
    service.refresh()

    assert store.known_filenames() == {"voice-8-abcdefabcdef.m4a"}


def test_an_upload_this_machine_received_is_adopted_at_once(tmp_path):
    # The upload endpoint lands files already keyed; its refresh names the
    # key so the settle wait never delays a phone push.
    store = MemoStore(tmp_path / "memos.db")
    service = _keyed_service(tmp_path, store, settle=5)

    service.refresh(adopt_now="voice-8-abcdefabcdef.m4a")

    assert store.known_filenames() == {"voice-8-abcdefabcdef.m4a"}
