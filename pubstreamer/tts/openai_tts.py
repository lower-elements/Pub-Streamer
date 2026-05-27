"""OpenAI TTS engine via REST API (no openai package required)."""

from .base import TtsEngine, decode_audio_bytes, pcm16_to_wav

_API_URL = "https://api.openai.com/v1/audio/speech"

VOICES = ["alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"]
MODELS = ["tts-1", "tts-1-hd"]


class OpenAITtsEngine(TtsEngine):
    name = "OpenAI"

    VOICES = VOICES
    MODELS = MODELS

    def __init__(self, api_key: str = "", model: str = "tts-1",
                 voice: str = "alloy", speed: float = 1.0):
        self.api_key = api_key
        self.model   = model
        self.voice   = voice
        self.speed   = speed

    def is_available(self) -> bool:
        try:
            import httpx  # noqa: F401
            return True
        except ImportError:
            return False

    def synthesize(self, text: str, sample_rate: int, channels: int):
        if not self.api_key:
            print("[OpenAI TTS] API key not configured", flush=True)
            return None
        try:
            import httpx
            resp = httpx.post(
                _API_URL,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model":           self.model,
                    "voice":           self.voice,
                    "input":           text,
                    "speed":           max(0.25, min(4.0, self.speed)),
                    "response_format": "pcm",   # signed 16-bit LE mono 24 kHz
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            wav = pcm16_to_wav(resp.content, 24000, 1)
            return decode_audio_bytes(wav, sample_rate, channels)
        except Exception as e:
            print(f"[OpenAI TTS] synthesize error: {e}", flush=True)
            return None

    def get_config(self) -> dict:
        return {"api_key": self.api_key, "model": self.model,
                "voice": self.voice, "speed": self.speed}

    def set_config(self, cfg: dict):
        self.api_key = cfg.get("api_key", "")
        self.model   = cfg.get("model",   "tts-1")
        self.voice   = cfg.get("voice",   "alloy")
        self.speed   = float(cfg.get("speed", 1.0))
