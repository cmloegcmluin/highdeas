import json
import threading
from pathlib import Path

from highdeas.ingest import NewRecording, recording_key
from highdeas.service import InboxService
from highdeas.store import Memo, MemoStore
from highdeas.transcribe import TimedWord, Transcript


class FakeTranscriber:
    def transcribe(self, path):
        return Transcript(f"text for {Path(path).name}")


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

    assert {m.audio_filename for m in store.list_by_status("pending")} == {
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

    pending = {m.audio_filename for m in store.list_by_status("pending")}
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

    pending = store.list_by_status("pending")
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

    pending = store.list_by_status("pending")
    assert len(pending) == 1
    memo = pending[0]
    assert memo.processed_at == ""
    assert not (bin_dir / "a.m4a").exists()  # left the bin
    assert (inbox / memo.audio_filename).read_bytes() == b"A"  # back in the inbox, playable


def test_restore_drops_a_memos_old_position_so_it_lands_at_the_end(tmp_path):
    # A memo carries the slot it was dragged into. Coming back from the bin it must
    # forget it, or it reappears in the middle of a since-rearranged inbox.
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "old.m4a").write_bytes(b"OLD")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="here.m4a", status="pending"))
    store.upsert(Memo(audio_filename="old.m4a", status="deleted", processed_at="2026-07-07T03:00"))
    store.reorder(["old.m4a", "here.m4a"])  # old.m4a used to lead the inbox

    InboxService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
                  bin_dir=bin_dir).restore("old.m4a")

    # Restore re-keys the recording, so identify it by what it isn't: it trails the
    # memo that stayed, instead of reclaiming its old leading slot.
    pending = store.list_by_status("pending")
    assert len(pending) == 2
    assert pending[0].audio_filename == "here.m4a"


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

    pending = store.list_by_status("pending")
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
    assert [m.audio_filename for m in store.list_by_status("pending")] == [key]
    assert store.get(key).transcript == "the idea"  # the kept memo, with its edits
    assert (inbox / key).read_bytes() == b"SAME-RECORDING"


def test_refresh_leaves_a_raw_named_pending_memo_already_in_the_inbox_untouched(tmp_path):
    """Refresh must not re-ingest a memo that is already pending with its audio in
    the inbox. A memo stored under a raw (pre-content-key) name is the trap: its
    content key differs from its filename, so find_new mistakes it for a brand-new
    recording and re-transcribes it — hanging 'Back to inbox' (or 500ing) and
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
    service.refresh()  # the "Back to inbox" reload that re-scans the inbox

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

    assert len(store.list_by_status("pending")) == 2
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
    assert len(store.list_by_status("pending")) == 1


def test_has_incoming_is_true_when_the_inbox_holds_an_untranscribed_recording(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "voice.m4a").write_bytes(b"A")
    store = MemoStore(tmp_path / "memos.db")
    service = InboxService(inbox_dir=inbox, store=store,
                            transcriber=FakeTranscriber(), bin_dir=tmp_path / "bin")

    assert service.has_incoming() is True


def test_has_incoming_is_false_when_the_inbox_is_drained(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    store = MemoStore(tmp_path / "memos.db")
    service = InboxService(inbox_dir=inbox, store=store,
                            transcriber=FakeTranscriber(), bin_dir=tmp_path / "bin")

    assert service.has_incoming() is False


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
