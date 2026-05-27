"""Piper neural TTS engine via the `piper-tts` pip package."""

import numpy as np

from .base import TtsEngine, decode_audio_bytes, pcm16_to_wav


class PiperEngine(TtsEngine):
    name = "Piper"

    def __init__(self, model_path: str = ""):
        self.model_path = model_path
        self._voice = None   # cached PiperVoice; reloaded if model_path changes
        self._loaded_path: str = ""

    def is_available(self) -> bool:
        try:
            import piper  # noqa: F401
            return True
        except ImportError:
            return False

    def _load(self):
        if self._voice is None or self._loaded_path != self.model_path:
            from piper import PiperVoice
            self._voice = PiperVoice.load(self.model_path)
            self._loaded_path = self.model_path

    def synthesize(self, text: str, sample_rate: int, channels: int):
        if not self.model_path:
            print("[Piper] no model path configured", flush=True)
            return None
        try:
            self._load()
            # piper-tts >= 1.4 returns AudioChunk objects with audio_float_array.
            chunks = list(self._voice.synthesize(text))
            if not chunks:
                return None
            native_sr = chunks[0].sample_rate
            mono = np.concatenate([c.audio_float_array for c in chunks])
            pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            wav = pcm16_to_wav(pcm, native_sr, 1)
            return decode_audio_bytes(wav, sample_rate, channels)
        except Exception as e:
            print(f"[Piper] synthesize error: {e}", flush=True)
            return None

    def get_config(self) -> dict:
        return {"model_path": self.model_path}

    def set_config(self, cfg: dict):
        new_path = cfg.get("model_path", "")
        if new_path != self.model_path:
            self._voice = None
        self.model_path = new_path
