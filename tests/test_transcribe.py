import subprocess

import pytest

from highdeas.transcribe import AudioDecodeError, Transcriber, decode_to_wav


class FakeRunner:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []
        self.kwargs = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        self.kwargs.append(kwargs)
        return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr=self.stderr)


def test_decode_to_wav_builds_16k_mono_command_and_returns_wav_path(tmp_path):
    runner = FakeRunner()

    out = decode_to_wav(tmp_path / "voice.m4a", out_dir=tmp_path, ffmpeg_exe="ff", runner=runner)

    assert out == tmp_path / "voice.wav"
    assert runner.calls[0] == [
        "ff", "-y", "-i", str(tmp_path / "voice.m4a"),
        "-ar", "16000", "-ac", "1", str(tmp_path / "voice.wav"),
    ]


def test_decode_to_wav_locates_ffmpeg_when_not_given(tmp_path):
    runner = FakeRunner()

    decode_to_wav(tmp_path / "voice.m4a", out_dir=tmp_path, runner=runner,
                  locate_ffmpeg=lambda: "LOCATED_FFMPEG")

    assert runner.calls[0][0] == "LOCATED_FFMPEG"


def test_decode_to_wav_raises_on_ffmpeg_failure(tmp_path):
    runner = FakeRunner(returncode=1, stderr="Invalid data found when processing input")

    with pytest.raises(AudioDecodeError, match="Invalid data found"):
        decode_to_wav(tmp_path / "bad.m4a", out_dir=tmp_path, ffmpeg_exe="ff", runner=runner)


def test_decode_to_wav_hides_console_window(tmp_path):
    """ffmpeg must run without flashing a console window (CREATE_NO_WINDOW on Windows)."""
    runner = FakeRunner()

    decode_to_wav(tmp_path / "voice.m4a", out_dir=tmp_path, ffmpeg_exe="ff", runner=runner)

    assert runner.kwargs[0].get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Recognition:
    """What onnx-asr's timestamped adapter hands back: the text, plus the sub-word
    tokens it decoded and the second each was emitted at."""

    def __init__(self, text, tokens=(), timestamps=()):
        self.text = text
        self.tokens = tokens
        self.timestamps = timestamps


class FakeModel:
    def __init__(self, result):
        self.result = result
        self.recognized = []

    def recognize(self, wav):
        self.recognized.append(wav)
        return self.result


def test_transcriber_decodes_then_recognizes(tmp_path):
    model = FakeModel(Recognition("hello world"))
    decoded = tmp_path / "voice.wav"
    decoded_calls = []

    def fake_decode(src):
        decoded_calls.append(src)
        return decoded

    spoken = Transcriber(model=model, decode=fake_decode).transcribe(tmp_path / "voice.m4a")

    assert spoken.text == "hello world"
    assert decoded_calls == [tmp_path / "voice.m4a"]
    assert model.recognized == [decoded]


def test_transcriber_lazy_loads_model_once(tmp_path):
    model = FakeModel(Recognition("hi"))
    load_calls = []

    def fake_loader(name):
        load_calls.append(name)
        return model

    transcriber = Transcriber(
        model_loader=fake_loader,
        model_name="the-model",
        decode=lambda src: tmp_path / "x.wav",
    )
    assert load_calls == []  # nothing loaded until first use

    transcriber.transcribe(tmp_path / "a.m4a")
    transcriber.transcribe(tmp_path / "b.m4a")

    assert load_calls == ["the-model"]  # loaded exactly once, by name


def test_transcriber_groups_sub_word_tokens_into_words_it_can_time(tmp_path):
    # The model emits sub-word tokens, each stamped with the second it was spoken:
    # a leading space starts a new word, everything else continues the one before.
    # The editor highlights whole words, so gather them here rather than there.
    model = FakeModel(Recognition(
        "I need a dusting.",
        tokens=[" I", " need", " a", " d", "ust", "ing", "."],
        timestamps=[0.96, 1.52, 2.08, 2.32, 2.48, 2.72, 2.88],
    ))

    spoken = Transcriber(model=model, decode=lambda src: tmp_path / "x.wav").transcribe(tmp_path / "a.m4a")

    assert [(w.start, w.text) for w in spoken.words] == [
        (0.96, "I"), (1.52, "need"), (2.08, "a"), (2.32, "dusting."),
    ]


def test_transcriber_reports_no_words_when_the_model_gives_no_timings(tmp_path):
    # A model without timestamp support still transcribes; the note just can't
    # highlight along with its audio.
    model = FakeModel(Recognition("hello world", tokens=None, timestamps=None))

    spoken = Transcriber(model=model, decode=lambda src: tmp_path / "x.wav").transcribe(tmp_path / "a.m4a")

    assert spoken.text == "hello world"
    assert spoken.words == ()
