"""Application service: turn the inbox into reviewable memos and route submissions."""
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from voicememo.ingest import find_new_recordings, recording_key, recording_time
from voicememo.store import Memo


def _no_router(memo):
    """Placeholder until the Notesnook / Drive routers are wired in."""


def _now():
    return datetime.now().isoformat(timespec="seconds")


class ReviewService:
    def __init__(self, *, inbox_dir, store, transcriber, bin_dir,
                 find_new=find_new_recordings, route=_no_router, clock=_now,
                 recorded_time=recording_time):
        self._inbox_dir = inbox_dir
        self._store = store
        self._transcriber = transcriber
        self._bin_dir = bin_dir
        self._find_new = find_new
        self._route = route
        self._clock = clock
        self._recorded_time = recorded_time

    def refresh(self):
        self.purge_expired()
        for recording in self._find_new(self._inbox_dir, self._store.known_filenames()):
            adopted = self._adopt(recording)
            self._store.upsert(Memo(
                audio_filename=recording.name,
                transcript=self._transcriber.transcribe(adopted),
                status="pending",
                created_at=self._clock(),
                recorded_at=self._recorded_time(adopted),
            ))

    def pending(self):
        return self._store.list_by_status("pending")

    def binned(self):
        """Processed/deleted memos whose recording sits in the local bin, newest first."""
        bin_path = Path(self._bin_dir)
        present = {p.name for p in bin_path.iterdir()} if bin_path.exists() else set()
        retired = self._store.list_by_status("processed") + self._store.list_by_status("deleted")
        in_bin = [memo for memo in retired if memo.audio_filename in present]
        return sorted(in_bin, key=lambda memo: memo.processed_at, reverse=True)

    def edit(self, audio_filename, **fields):
        self._store.update(audio_filename, **fields)

    def submit(self, audio_filename):
        self._route(self._store.get(audio_filename))
        self._retire_audio(audio_filename)
        self._store.update(audio_filename, status="processed", processed_at=self._clock())

    def delete(self, audio_filename):
        self._retire_audio(audio_filename)
        self._store.update(audio_filename, status="deleted", processed_at=self._clock())

    def restore(self, audio_filename):
        """Bring a binned recording back into the inbox as a pending memo.

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
            self._store.rekey(audio_filename, key)
        self._store.update(key, status="pending", processed_at="")

    def purge_expired(self, *, retention_days=90):
        """Forget bin items older than the retention window: delete the audio and the record."""
        cutoff = datetime.fromisoformat(self._clock()) - timedelta(days=retention_days)
        bin_dir = Path(self._bin_dir)
        for memo in self._store.list_by_status("processed") + self._store.list_by_status("deleted"):
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

    def _retire_audio(self, audio_filename):
        """Take the recording out of the inbox, unless the route already moved it (Drive)."""
        source = Path(self._inbox_dir) / audio_filename
        if source.exists():
            bin_dir = Path(self._bin_dir)
            bin_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(bin_dir / audio_filename))
