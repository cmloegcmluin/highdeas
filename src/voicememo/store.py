"""SQLite-backed store for memo review state (transcript, name, route, status)."""
import sqlite3
import threading
from dataclasses import dataclass, fields


@dataclass
class Memo:
    audio_filename: str
    transcript: str = ""
    name: str = ""
    route: str = "notesnook"
    status: str = "pending"
    created_at: str = ""
    recorded_at: str = ""
    processed_at: str = ""


_COLUMNS = [f.name for f in fields(Memo)]


def _row_to_memo(row):
    return Memo(**{c: row[c] for c in _COLUMNS})


class MemoStore:
    """Thread-safe: the Flask dev server serves each request in its own thread."""

    def __init__(self, db_path):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        columns = ", ".join(f"{c} TEXT" for c in _COLUMNS)
        with self._lock:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS memos ({columns}, PRIMARY KEY (audio_filename))"
            )
            present = {row["name"] for row in self._conn.execute("PRAGMA table_info(memos)")}
            for column in _COLUMNS:
                if column not in present:
                    self._conn.execute(f"ALTER TABLE memos ADD COLUMN {column} TEXT")
            self._conn.commit()

    def upsert(self, memo):
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO memos ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                [getattr(memo, c) for c in _COLUMNS],
            )
            self._conn.commit()

    def get(self, audio_filename):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memos WHERE audio_filename = ?", (audio_filename,)
            ).fetchone()
        return _row_to_memo(row) if row is not None else None

    def known_filenames(self):
        with self._lock:
            rows = self._conn.execute("SELECT audio_filename FROM memos").fetchall()
        return {row["audio_filename"] for row in rows}

    def list_by_status(self, status):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memos WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        return [_row_to_memo(row) for row in rows]

    def update(self, audio_filename, **changes):
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self._lock:
            self._conn.execute(
                f"UPDATE memos SET {assignments} WHERE audio_filename = ?",
                [*changes.values(), audio_filename],
            )
            self._conn.commit()

    def rekey(self, old_filename, new_filename):
        """Move a memo to a new audio_filename (its primary key), keeping the rest."""
        with self._lock:
            self._conn.execute(
                "UPDATE memos SET audio_filename = ? WHERE audio_filename = ?",
                (new_filename, old_filename),
            )
            self._conn.commit()

    def remove(self, audio_filename):
        with self._lock:
            self._conn.execute("DELETE FROM memos WHERE audio_filename = ?", (audio_filename,))
            self._conn.commit()
