"""Microsoft Edge online TTS engine via the edge-tts package."""

import asyncio

from pubstreamer.tts.base import TtsEngine, decode_audio_bytes

_DEFAULT_VOICE = "en-US-AriaNeural"


def _pct(n: int) -> str:
    """Convert integer to SSML percentage string, e.g. 10 → '+10%', -20 → '-20%'."""
    return f"+{n}%" if n >= 0 else f"{n}%"


def _hz(n: int) -> str:
    """Convert integer to SSML Hz string, e.g. 10 → '+10Hz'."""
    return f"+{n}Hz" if n >= 0 else f"{n}Hz"


class EdgeEngine(TtsEngine):
    name = "Edge TTS"
    key  = "edge"

    CONFIG_SCHEMA = [
        {"key": "voice",  "label": "Voice:",   "type": "voice_list",
         "fetch": "fetch_voices"},
        {"key": "rate",   "label": "Rate:",    "type": "slider",
         "min": -50, "max": 100, "fmt": "pct_signed", "default": 0},
        {"key": "volume", "label": "Volume:",  "type": "slider",
         "min": -50, "max": 100, "fmt": "pct_signed", "default": 0},
        {"key": "pitch",  "label": "Pitch:",   "type": "slider",
         "min": -50, "max": 50, "fmt": "hz_signed", "default": 0},
        {"type": "note",
         "text": "Uses Microsoft Edge read-aloud service. Requires internet."},
    ]

    def __init__(self, voice: str = _DEFAULT_VOICE,
                 rate: int = 0, volume: int = 0, pitch: int = 0):
        self.voice  = voice
        self.rate   = rate    # integer percentage offset (-50 … +100)
        self.volume = volume  # integer percentage offset (-50 … +100)
        self.pitch  = pitch   # integer Hz offset (-50 … +50)

    def is_available(self) -> bool:
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False

    def synthesize(self, text: str, sample_rate: int, channels: int):
        try:
            import edge_tts

            async def _run() -> bytes:
                communicate = edge_tts.Communicate(
                    text, self.voice,
                    rate=_pct(self.rate),
                    volume=_pct(self.volume),
                    pitch=_hz(self.pitch),
                )
                audio = b""
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio += chunk["data"]
                return audio

            audio_bytes = asyncio.run(_run())
            if not audio_bytes:
                return None
            return decode_audio_bytes(audio_bytes, sample_rate, channels)
        except Exception as e:
            print(f"[Edge TTS] synthesize error: {e}", flush=True)
            return None

    @classmethod
    def fetch_voices(cls, config: dict) -> list[tuple[str, str]]:
        """Return sorted list of (short_name, display_label) pairs."""
        try:
            import edge_tts

            voices = asyncio.run(edge_tts.list_voices())
            pairs = [
                (v["ShortName"], f"{v['ShortName']}  ({v.get('Gender', '?')})")
                for v in voices
            ]
            return sorted(pairs, key=lambda x: x[0])
        except Exception as e:
            raise RuntimeError(f"Edge voice list failed: {e}") from e

    def get_config(self) -> dict:
        return {"voice": self.voice, "rate": self.rate,
                "volume": self.volume, "pitch": self.pitch}

    def set_config(self, cfg: dict):
        self.voice  = cfg.get("voice",  _DEFAULT_VOICE)
        self.rate   = int(cfg.get("rate",   0))
        self.volume = int(cfg.get("volume", 0))
        self.pitch  = int(cfg.get("pitch",  0))
