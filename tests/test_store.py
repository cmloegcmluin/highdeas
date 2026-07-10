import sqlite3
import threading

from highdeas.store import Memo, MemoStore


def test_upsert_then_get_roundtrips(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    memo = Memo(
        audio_filename="voice-4.m4a",
        transcript="hello",
        name="An idea",
        created_at="2026-07-07T02:12:00",
    )

    store.upsert(memo)

    assert store.get("voice-4.m4a") == memo


def test_recorded_at_roundtrips(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", recorded_at="2026-07-07T13:37:04"))

    assert store.get("a.m4a").recorded_at == "2026-07-07T13:37:04"


def test_get_unknown_returns_none(tmp_path):
    store = MemoStore(tmp_path / "memos.db")

    assert store.get("nope.m4a") is None


def test_store_migrates_a_db_created_before_the_newer_columns_existed(tmp_path):
    # Opening a memos.db from an older version must add every column since added,
    # each with the type it needs — position sorts numerically only as an INTEGER.
    db = tmp_path / "memos.db"
    legacy = sqlite3.connect(db)
    legacy.execute(
        "CREATE TABLE memos (audio_filename TEXT PRIMARY KEY, transcript TEXT, "
        "name TEXT, route TEXT, status TEXT, created_at TEXT, processed_at TEXT)"
    )
    legacy.commit()
    legacy.close()

    store = MemoStore(db)
    names = [f"{i}.m4a" for i in range(12)]
    for filename in names:
        store.upsert(Memo(audio_filename=filename, status="pending",
                          recorded_at="2026-07-07T13:37:04"))
    store.reorder(names)

    assert store.get("0.m4a").recorded_at == "2026-07-07T13:37:04"
    assert [m.audio_filename for m in store.list_by_status("pending")] == names


def test_known_filenames_returns_stored_filenames(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a"))
    store.upsert(Memo(audio_filename="b.m4a"))

    assert store.known_filenames() == {"a.m4a", "b.m4a"}


def test_list_by_status_filters_and_orders_by_recorded_at(tmp_path):
    # Order by when each memo was recorded, not when it was ingested, so the inbox
    # list always reads oldest-to-newest. Ingestion order can't be trusted: a startup
    # catch-up scans the inbox by filename (voice-10 before voice-2), which is neither
    # recording order nor consistent with the live poll's arrival order.
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="b.m4a", status="pending",
                      recorded_at="2026-07-07T02:00", created_at="2026-07-07T08:00"))
    store.upsert(Memo(audio_filename="a.m4a", status="pending",
                      recorded_at="2026-07-07T01:00", created_at="2026-07-07T09:00"))
    store.upsert(Memo(audio_filename="done.m4a", status="processed",
                      recorded_at="2026-07-07T03:00"))

    pending = store.list_by_status("pending")

    # a was recorded first though ingested last, so it still sorts ahead of b.
    assert [m.audio_filename for m in pending] == ["a.m4a", "b.m4a"]


def test_reorder_pins_pending_memos_to_the_given_order(tmp_path):
    # Dragging a row rewrites the pending order, overriding recorded time.
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", status="pending", recorded_at="2026-07-07T01:00"))
    store.upsert(Memo(audio_filename="b.m4a", status="pending", recorded_at="2026-07-07T02:00"))
    store.upsert(Memo(audio_filename="c.m4a", status="pending", recorded_at="2026-07-07T03:00"))

    store.reorder(["c.m4a", "a.m4a", "b.m4a"])

    assert [m.audio_filename for m in store.list_by_status("pending")] == ["c.m4a", "a.m4a", "b.m4a"]


def test_a_memo_with_no_position_lists_after_the_reordered_ones(tmp_path):
    # A recording that arrives after the user has arranged the inbox joins the end,
    # rather than jumping into the middle on its recorded time.
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", status="pending", recorded_at="2026-07-07T01:00"))
    store.upsert(Memo(audio_filename="b.m4a", status="pending", recorded_at="2026-07-07T02:00"))
    store.reorder(["b.m4a", "a.m4a"])

    store.upsert(Memo(audio_filename="fresh.m4a", status="pending", recorded_at="2026-07-07T00:30"))

    assert [m.audio_filename for m in store.list_by_status("pending")] == [
        "b.m4a", "a.m4a", "fresh.m4a"]


def test_reorder_stays_numeric_past_the_tenth_memo(tmp_path):
    # Positions must compare as numbers: as text, '10' would sort between '1' and '2'.
    store = MemoStore(tmp_path / "memos.db")
    names = [f"{i}.m4a" for i in range(12)]
    for filename in names:
        store.upsert(Memo(audio_filename=filename, status="pending"))

    store.reorder(names)

    assert [m.audio_filename for m in store.list_by_status("pending")] == names


def test_update_changes_named_fields_only(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a", name="", transcript="raw", route="notesnook"))

    store.update("a.m4a", name="Better name", transcript="edited", route="drive")

    memo = store.get("a.m4a")
    assert memo.name == "Better name"
    assert memo.transcript == "edited"
    assert memo.route == "drive"
    assert memo.status == "pending"  # untouched


def test_store_is_usable_from_another_thread(tmp_path):
    # The Flask dev server handles each request in a new thread, so the store
    # must not be pinned to the thread that created it.
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a"))
    result = {}

    def worker():
        try:
            result["names"] = store.known_filenames()
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert result.get("error") is None, result.get("error")
    assert result["names"] == {"a.m4a"}


def test_rekey_changes_the_primary_key_keeping_other_fields(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="raw.m4a", transcript="t", name="n", status="deleted"))

    store.rekey("raw.m4a", "raw-abc123abc123.m4a")

    assert store.get("raw.m4a") is None
    memo = store.get("raw-abc123abc123.m4a")
    assert memo.transcript == "t"
    assert memo.name == "n"
    assert memo.status == "deleted"


def test_remove_deletes_the_record(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a"))

    store.remove("a.m4a")

    assert store.get("a.m4a") is None
