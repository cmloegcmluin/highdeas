"""Transcribe voice-memo audio locally with NVIDIA Parakeet (via onnx-asr)."""
import subprocess
import tempfile
from pathlib import Path


# Keep ffmpeg from flashing a console window on Windows; a no-op (0) elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AudioDecodeError(Exception):
    """Raised when ffmpeg fails to decode an audio file."""


def _default_ffmpeg():
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


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


def _load_parakeet(name):
    import onnx_asr

    return onnx_asr.load_model(name)


class Transcriber:
    def __init__(self, *, model=None, decode=decode_to_wav,
                 model_loader=_load_parakeet, model_name=DEFAULT_MODEL):
        self._model = model
        self._decode = decode
        self._model_loader = model_loader
        self._model_name = model_name

    def _get_model(self):
        if self._model is None:
            self._model = self._model_loader(self._model_name)
        return self._model

    def transcribe(self, audio_path):
        wav = self._decode(audio_path)
        return self._get_model().recognize(wav)
