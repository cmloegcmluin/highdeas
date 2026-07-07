from voicememo.ingest import find_new_recordings


def test_finds_new_audio_excluding_known_and_non_audio(tmp_path):
    (tmp_path / "voice.m4a").write_bytes(b"a")
    (tmp_path / "voice-2.m4a").write_bytes(b"b")
    (tmp_path / "notes.txt").write_text("not audio")

    result = find_new_recordings(tmp_path, known_names={"voice.m4a"})

    assert [p.name for p in result] == ["voice-2.m4a"]


def test_missing_inbox_returns_empty(tmp_path):
    assert find_new_recordings(tmp_path / "does_not_exist", known_names=set()) == []
