"""Discover new voice-memo recordings dropped into the inbox folder."""
from pathlib import Path

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".caf", ".aiff"}


def find_new_recordings(inbox_dir, known_names):
    inbox = Path(inbox_dir)
    if not inbox.is_dir():
        return []
    new = [
        entry
        for entry in inbox.iterdir()
        if entry.is_file()
        and entry.suffix.lower() in AUDIO_EXTENSIONS
        and entry.name not in known_names
    ]
    return sorted(new, key=lambda p: p.name)
