"""Abstract TTS engine base class and shared audio decoding utilities."""

import io
import wave
import numpy as np


def decode_audio_bytes(audio_bytes: bytes, target_sr: int, channels: int) -> np.ndarray:
    """
    Decode any pedalboard-supported audio format (WAV, MP3, OGG, FLAC…) to
    a float32 ndarray of shape (channels, frames) resampled to target_sr.
    """
    from pedalboard.io import AudioFile
    with AudioFile(io.BytesIO(audio_bytes)).resampled_to(target_sr) as f:
        data = f.read(f.frames)          # (n_ch, frames) float32
    data = _normalise_channels(data, channels).astype(np.float32)
    np.clip(data, -1.0, 1.0, out=data)
    return data


def pcm16_to_wav(pcm: bytes, sr: int, ch: int) -> bytes:
    """Wrap raw 16-bit little-endian PCM in a RIFF WAV header."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return buf.getvalue()


def _normalise_channels(data: np.ndarray, target_ch: int) -> np.ndarray:
    got = data.shape[0]
    if got == target_ch:
        return data
    if got == 1 and target_ch == 2:
        return np.vstack([data, data])
    if got > target_ch:
        return data[:target_ch, :]
    # e.g. got=2 target=1: downmix
    return data.mean(axis=0, keepdims=True)


class TtsEngine:
    """
    Abstract base.  Subclasses implement synthesize() and optionally list_voices(),
    get_config(), set_config(), and is_available().
    """

    name: str = "Unknown"

    # Optional: set this to a stable ASCII key used in saved configs.
    # If unset, the key is auto-derived from name (lower-case, spaces→underscores).
    key: str | None = None

    CONFIG_SCHEMA: list[dict] = []

    @classmethod
    def fetch_voices(cls, config: dict) -> list[tuple[str, str]]:
        return []

    def is_available(self) -> bool:
        """Return True if the required optional packages are installed."""
        return True

    def synthesize(self, text: str, sample_rate: int, channels: int) -> "np.ndarray | None":
        """
        Convert text to a float32 ndarray of shape (channels, frames).
        Returns None on error.  May block for network/inference time.
        """
        raise NotImplementedError

    def list_voices(self) -> list[str]:
        return []

    def get_config(self) -> dict:
        """Return a JSON-serialisable dict of all configurable parameters."""
        return {}

    def set_config(self, cfg: dict):
        """Restore parameters from a previously saved get_config() dict."""
        pass
