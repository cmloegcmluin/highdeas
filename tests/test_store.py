import sqlite3
import threading

from voicememo.store import Memo, MemoStore


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


def test_store_migrates_a_db_created_before_recorded_at_existed(tmp_path):
    db = tmp_path / "memos.db"
    legacy = sqlite3.connect(db)
    legacy.execute(
        "CREATE TABLE memos (audio_filename TEXT PRIMARY KEY, transcript TEXT, "
        "name TEXT, route TEXT, status TEXT, created_at TEXT, processed_at TEXT)"
    )
    legacy.commit()
    legacy.close()

    store = MemoStore(db)  # opening the older DB must add the missing column
    store.upsert(Memo(audio_filename="a.m4a", recorded_at="2026-07-07T13:37:04"))

    assert store.get("a.m4a").recorded_at == "2026-07-07T13:37:04"


def test_known_filenames_returns_stored_filenames(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="a.m4a"))
    store.upsert(Memo(audio_filename="b.m4a"))

    assert store.known_filenames() == {"a.m4a", "b.m4a"}


def test_list_by_status_filters_and_orders_by_created_at(tmp_path):
    store = MemoStore(tmp_path / "memos.db")
    store.upsert(Memo(audio_filename="b.m4a", status="pending", created_at="2026-07-07T02:00"))
    store.upsert(Memo(audio_filename="a.m4a", status="pending", created_at="2026-07-07T01:00"))
    store.upsert(Memo(audio_filename="done.m4a", status="processed", created_at="2026-07-07T03:00"))

    pending = store.list_by_status("pending")

    assert [m.audio_filename for m in pending] == ["a.m4a", "b.m4a"]


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
