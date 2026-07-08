from pathlib import Path

from voicememo.ingest import NewRecording
from voicememo.service import ReviewService
from voicememo.store import Memo, MemoStore


class FakeTranscriber:
    def transcribe(self, path):
        return f"text for {Path(path).name}"


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

    service = ReviewService(
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

    service = ReviewService(inbox_dir=inbox, store=store,
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


def test_submit_routes_then_marks_processed(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", route="drive", status="pending"))
    routed = []

    service = ReviewService(
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

    service = ReviewService(
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

    service = ReviewService(
        inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
        bin_dir=bin_dir, route=lambda memo: None, clock=lambda: "T",
    )
    service.submit("a.m4a")

    assert not (inbox / "a.m4a").exists()
    assert (bin_dir / "a.m4a").read_bytes() == b"AUDIO"
    assert store.get("a.m4a").status == "processed"


def test_submit_leaves_bin_untouched_when_route_already_moved_audio(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    (inbox / "a.m4a").write_bytes(b"AUDIO")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", route="drive", status="pending"))

    def drive_route(memo):  # a Drive route moves the audio out of the inbox itself
        (inbox / "a.m4a").rename(tmp_path / "moved_to_drive.m4a")

    service = ReviewService(
        inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
        bin_dir=bin_dir, route=drive_route, clock=lambda: "T",
    )
    service.submit("a.m4a")

    assert not bin_dir.exists() or list(bin_dir.iterdir()) == []


def test_delete_retires_inbox_audio_to_bin(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    bin_dir = tmp_path / "bin"
    (inbox / "a.m4a").write_bytes(b"AUDIO")
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", status="pending"))

    ReviewService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
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

    service = ReviewService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(), bin_dir=bin_dir)

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

    ReviewService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(), bin_dir=bin_dir).restore("a.m4a")

    pending = store.list_by_status("pending")
    assert len(pending) == 1
    memo = pending[0]
    assert memo.processed_at == ""
    assert not (bin_dir / "a.m4a").exists()  # left the bin
    assert (inbox / memo.audio_filename).read_bytes() == b"A"  # back in the inbox, playable


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

    service = ReviewService(inbox_dir=inbox, store=store,
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

    service = ReviewService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
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

    service = ReviewService(inbox_dir=inbox, store=store, transcriber=FakeTranscriber(),
                            bin_dir=bin_dir, find_new=lambda inbox, known: [],
                            clock=lambda: "2026-07-07T00:00:00")
    service.refresh()

    assert store.get("old.m4a") is None
    assert not (bin_dir / "old.m4a").exists()
