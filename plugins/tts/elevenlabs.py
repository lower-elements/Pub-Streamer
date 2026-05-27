"""ElevenLabs TTS engine via REST API."""

from pubstreamer.tts.base import TtsEngine, decode_audio_bytes

_API_BASE         = "https://api.elevenlabs.io/v1"
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"   # Rachel
_DEFAULT_MODEL    = "eleven_multilingual_v2"


class ElevenLabsEngine(TtsEngine):
    name = "ElevenLabs"

    MODELS = [
        "eleven_v3",
        "eleven_multilingual_v2",
        "eleven_turbo_v2_5",
        "eleven_turbo_v2",
        "eleven_monolingual_v1",
    ]

    CONFIG_SCHEMA = [
        {"key": "api_key",          "label": "API key:",    "type": "text", "password": True},
        {"key": "model_id",         "label": "Model:",      "type": "choice",
         "choices": MODELS},
        {"key": "voice_id",         "label": "Voice:",      "type": "voice_list",
         "fetch": "fetch_voices"},
        {"key": "stability",        "label": "Stability:",  "type": "slider",
         "min": 0, "max": 100, "scale": 100.0, "default": 50},
        {"key": "similarity_boost", "label": "Similarity:", "type": "slider",
         "min": 0, "max": 100, "scale": 100.0, "default": 75},
        {"key": "speed",            "label": "Speed:",      "type": "slider",
         "min": 70, "max": 120, "scale": 100.0, "default": 100},
    ]

    def __init__(self, api_key: str = "", voice_id: str = _DEFAULT_VOICE_ID,
                 model_id: str = _DEFAULT_MODEL, stability: float = 0.5,
                 similarity_boost: float = 0.75, speed: float = 1.0):
        self.api_key          = api_key
        self.voice_id         = voice_id
        self.model_id         = model_id
        self.stability        = stability
        self.similarity_boost = similarity_boost
        self.speed            = speed

    def is_available(self) -> bool:
        try:
            import httpx  # noqa: F401
            return True
        except ImportError:
            return False

    def synthesize(self, text: str, sample_rate: int, channels: int):
        if not self.api_key:
            print("[ElevenLabs] API key not configured", flush=True)
            return None
        try:
            import httpx
            resp = httpx.post(
                f"{_API_BASE}/text-to-speech/{self.voice_id}",
                headers={"xi-api-key": self.api_key,
                         "Content-Type": "application/json"},
                json={
                    "text": text,
                    "model_id": self.model_id,
                    "voice_settings": {
                        "stability":        self.stability,
                        "similarity_boost": self.similarity_boost,
                        "speed":            self.speed,
                    },
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            return decode_audio_bytes(resp.content, sample_rate, channels)
        except Exception as e:
            print(f"[ElevenLabs] synthesize error: {e}", flush=True)
            return None

    @classmethod
    def fetch_voices(cls, config: dict) -> list[tuple[str, str]]:
        """Return sorted list of (voice_id, display_name) tuples."""
        api_key = config.get("api_key", "")
        try:
            import httpx
            resp = httpx.get(
                f"{_API_BASE}/voices",
                headers={"xi-api-key": api_key},
                timeout=15.0,
            )
            resp.raise_for_status()
            voices = resp.json().get("voices", [])
            return sorted([(v["voice_id"], v["name"]) for v in voices],
                          key=lambda x: x[1])
        except Exception as e:
            raise RuntimeError(f"ElevenLabs voice list failed: {e}") from e

    def get_config(self) -> dict:
        return {
            "api_key":          self.api_key,
            "voice_id":         self.voice_id,
            "model_id":         self.model_id,
            "stability":        self.stability,
            "similarity_boost": self.similarity_boost,
            "speed":            self.speed,
        }

    def set_config(self, cfg: dict):
        self.api_key          = cfg.get("api_key",          "")
        self.voice_id         = cfg.get("voice_id",         _DEFAULT_VOICE_ID)
        self.model_id         = cfg.get("model_id",         _DEFAULT_MODEL)
        self.stability        = float(cfg.get("stability",        0.5))
        self.similarity_boost = float(cfg.get("similarity_boost", 0.75))
        self.speed            = float(cfg.get("speed",            1.0))
