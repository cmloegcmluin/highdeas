"""Transcribe voice-memo audio locally with NVIDIA Parakeet (via onnx-asr)."""
import re
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy

from highdeas.audio import NO_WINDOW as _NO_WINDOW
from highdeas.audio import locate_ffmpeg as _default_ffmpeg
from highdeas.hesitation import without_hesitations
from highdeas.nonspeech import mark_nonspeech
from highdeas.vocabulary import corrections


class AudioDecodeError(Exception):
    """Raised when ffmpeg fails to decode an audio file."""


def decode_to_wav(src, *, out_dir=None, ffmpeg_exe=None, runner=subprocess.run,
                  locate_ffmpeg=_default_ffmpeg):
    if ffmpeg_exe is None:
        ffmpeg_exe = locate_ffmpeg()
    src = Path(src)
    out_dir = Path(out_dir) if out_dir is not None else Path(tempfile.gettempdir())
    out = out_dir / (src.stem + ".wav")
    cmd = [ffmpeg_exe, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(out)]
    result = runner(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if result.returncode != 0:
        raise AudioDecodeError(f"ffmpeg failed to decode {src.name}: {result.stderr}")
    return out


DEFAULT_MODEL = "nemo-parakeet-tdt-0.6b-v3"

# How much of a recording the model will take in one go. Its exported encoder carries
# a positional table 5000 frames wide — 400 seconds at the encoder's 80ms stride — and
# anything longer fails outright ("Attempting to broadcast an axis by a dimension other
# than 1"), on every scan, forever. So a longer recording is heard in pieces this size,
# which leaves room to spare under that ceiling.
HEARABLE_SECONDS = 360.0
# How far back from that ceiling to hunt for the quietest moment to end a piece on, and
# how long a stretch is judged for quiet. A seam that falls in a pause costs the
# transcript nothing; one through the middle of a word costs it that word twice, garbled
# at the end of one piece and again at the start of the next.
_PAUSE_HUNT_SECONDS = 45.0
_PAUSE_SECONDS = 0.25


@dataclass(frozen=True)
class TimedWord:
    """A spoken word and the second, into the recording, that it starts on."""
    start: float
    text: str


@dataclass(frozen=True)
class Recognition:
    """What the model made of a recording: the text, the sub-word tokens it decoded,
    and the second each was spoken at. The shape onnx-asr hands back, so a recording
    heard in pieces reads exactly like one heard in a single go."""
    text: str
    tokens: tuple = ()
    timestamps: tuple = ()


def _read_wav(path):
    """A decoded recording as float32 in [-1, 1], with its sample rate — what onnx-asr
    would read off the file itself, read here so a long one can be handed over a piece
    at a time. `decode_to_wav` is the only writer of these, and it writes 16-bit mono."""
    with wave.open(str(path), "rb") as recording:
        rate = recording.getframerate()
        frames = recording.readframes(recording.getnframes())
    return numpy.frombuffer(frames, dtype="<i2").astype(numpy.float32) / 32768.0, rate


def _quietest(samples, first, last, width):
    """The middle of the quietest `width`-long stretch of `samples[first:last]`."""
    at = min(range(first, last - width + 1, width),
             key=lambda start: numpy.abs(samples[start:start + width]).max())
    return at + width // 2


def _seams(samples, rate):
    """Where to cut a recording the model can't hear in one go, as sample offsets.

    Each piece runs as near the ceiling as it can while still ending on a pause, so
    every piece is hearable and no seam falls through the middle of a word."""
    span, hunt, pause = (int(seconds * rate) for seconds in
                         (HEARABLE_SECONDS, _PAUSE_HUNT_SECONDS, _PAUSE_SECONDS))
    seams, at = [], 0
    while len(samples) - at > span:
        at = _quietest(samples, at + span - hunt, at + span, pause)
        seams.append(at)
    return seams


def _pieces(samples, rate):
    """A recording in stretches the model can take, each paired with the second it
    starts at. One stretch — the whole recording — when it already fits."""
    edges = [0, *_seams(samples, rate), len(samples)]
    return [(at / rate, samples[at:until]) for at, until in zip(edges, edges[1:])]


class HearsAnyLength:
    """The ASR model, able to take a recording of any length.

    Past `HEARABLE_SECONDS` the model refuses a recording rather than shortening its
    answer, so a long one is heard in pieces and the pieces put back together — each
    piece's word timings slid to where in the recording that piece starts.

    Hearing one in pieces is also the only honest place to count how far along it is,
    which is what `progress` is called with after each: the fraction of the recording
    read so far, so the page has a real number to show rather than a guess at the
    clock. A recording that fits says so once, when it is read."""

    def __init__(self, model):
        self._model = model

    def recognize(self, wav, progress=None):
        samples, rate = _read_wav(wav)
        heard = []
        for at, piece in _pieces(samples, rate):
            heard.append((at, self._model.recognize(piece, sample_rate=rate)))
            if progress is not None:
                progress((at * rate + len(piece)) / len(samples))
        return Recognition(
            text=" ".join(said.text for _, said in heard if said.text),
            tokens=tuple(token for _, said in heard for token in said.tokens or ()),
            timestamps=tuple(round(at + stamp, 3) for at, said in heard
                             for stamp in said.timestamps or ()),
        )


@dataclass(frozen=True)
class Transcript:
    """What a recording said, and when it said each word."""
    text: str
    words: tuple = ()


def _load_parakeet(name):
    import onnx_asr

    # The timestamped adapter reports the sub-word tokens and their emission times
    # alongside the text, which is what lets the editor light up each word as the
    # recording plays. Transcription is CPU-by-design on every platform: left to
    # choose, onnxruntime picks CoreML on macOS, which fails to initialize this
    # external-data model ("model_path must not be empty").
    return HearsAnyLength(
        onnx_asr.load_model(name, providers=["CPUExecutionProvider"]).with_timestamps())


def _to_words(tokens, timestamps):
    """Gather the model's sub-word tokens into whole words with a start time.

    The model emits tokens like " d", "ust", "ing", ".", each stamped with the
    second it was spoken. A leading space starts a new word; everything else
    continues the word before it — including trailing punctuation, which belongs
    to the word it follows."""
    words = []
    for token, start in zip(tokens or (), timestamps or ()):
        if words and not token[:1].isspace():
            words[-1] = TimedWord(words[-1].start, words[-1].text + token)
        else:
            words.append(TimedWord(start, token.strip()))
    return tuple(word for word in words if word.text)


def _applied(tokens, fixes):
    """`tokens` as the `(index, length, replacement)` of `fixes` leave them, in the
    kept form `_rewritten` reads: each token paired with the index it came from, and
    each corrected run standing as the one term it missed, at the index the run began
    on — which is to say at the moment it began."""
    out, index = [], 0
    for at, size, replacement in fixes:
        out.extend((n, tokens[n]) for n in range(index, at))
        out.append((at, replacement))
        index = at + size
    out.extend((n, tokens[n]) for n in range(index, len(tokens)))
    return tuple(out)


def _respaced(text, kept):
    """`text` with only `kept` left of it: each survivor spelled as `kept` spells it,
    in the place it was, spaced from its neighbours exactly as it was.

    The spacing has to be carried rather than rebuilt. He dictates lists, and the model
    lays one out a line per item — join the surviving words with single spaces and his
    list comes back as a paragraph."""
    if not kept:
        return ""
    spans = [word.span() for word in re.finditer(r"\S+", text)]
    out = [text[:spans[0][0]]]
    for place, (index, token) in enumerate(kept):
        if place:
            out.append(text[spans[index - 1][1]:spans[index][0]])
        out.append(token)
    out.append(text[spans[-1][1]:])
    return "".join(out)


def _rewritten(spoken, edit):
    """`spoken` as `edit` rewrites it, in the text and in the word timings alike.

    `edit` reads a list of words and answers with the ones worth keeping, each paired
    with the index it came from. A kept word takes the timing of the index it answers
    with, so a run gathered into one term lights up from the moment the run began.

    The text and the timed words are two tellings of the same speech and need not agree
    word for word, so each is edited against itself rather than one being read off the
    other."""
    tokens = spoken.text.split()
    kept = edit(tokens)
    if kept == tuple(enumerate(tokens)):
        return spoken  # nothing to rewrite — hand back what came, spacing and all
    return Transcript(
        _respaced(spoken.text, kept),
        tuple(TimedWord(spoken.words[index].start, token) for index, token
              in edit([word.text for word in spoken.words])),
    )


def _corrected(spoken, terms):
    """`spoken` as it would read had the model known these terms: every near-miss of
    one swapped for the term it missed."""
    return _rewritten(spoken, lambda words: _applied(words, corrections(words, terms)))


class Transcriber:
    def __init__(self, *, model=None, decode=decode_to_wav,
                 model_loader=_load_parakeet, model_name=DEFAULT_MODEL,
                 read_terms=lambda: ()):
        self._model = model
        self._decode = decode
        self._model_loader = model_loader
        self._model_name = model_name
        self._read_terms = read_terms

    def _get_model(self):
        if self._model is None:
            self._model = self._model_loader(self._model_name)
        return self._model

    def transcribe(self, audio_path, progress=None):
        wav = self._decode(audio_path)
        recognized = self._get_model().recognize(wav, progress=progress)
        # He says "um" and "uh" while he thinks and the model writes both down. They go
        # first, so that a note that was nothing else is left empty for the next step
        # to read as [unclear].
        heard = Transcript(recognized.text,
                           _to_words(recognized.tokens, recognized.timestamps))
        spoken = _rewritten(heard, without_hesitations)
        # The model rarely returns nothing; it renders humming as filler and noise as a
        # confident hallucination. Relabel those as [singing]/[unclear] before storing.
        # A relabelled note drops its word timings — there are no real words to light up.
        marked = mark_nonspeech(spoken.text)
        if marked == spoken.text:
            return _corrected(spoken, self._read_terms())
        return Transcript(marked)
