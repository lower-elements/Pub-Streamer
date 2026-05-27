"""
Example TTS engine plugin for Pub-Streamer.

Drop a .py file into this directory containing a TtsEngine subclass.
It is discovered automatically at startup — no registration elsewhere required.

CONFIG_SCHEMA drives the UI panel for this engine. Supported field types:

  text     — single-line text input. Add "password": True for masked input.
  file     — read-only text + Browse button. Add "wildcard" for file filter.
  choice   — drop-down. "choices" is list[str] or list[tuple[id, label]].
  checkbox — boolean tick box.
  slider   — integer slider. "min"/"max" required. Optional: "scale" (divides
             stored value; e.g. scale=100.0 stores 0.00–1.00 from a 0–100
             slider), "fmt" ("pct_signed", "hz_signed", "scale_x").
  voice_list — scrollable list box. Static: set "choices" (same as choice).
               Fetched: set "fetch" to the name of a @classmethod on the
               engine that accepts (cls, config: dict) and returns
               list[tuple[id, label]]. The UI shows a "Get Available Voices"
               button that calls it in a background thread.
  note     — grey informational text. Use "text" key instead of "key"/"label".
"""

import numpy as np
from pubstreamer.tts.base import TtsEngine, decode_audio_bytes


class ExampleEngine(TtsEngine):
    name = "Example"   # shown in the engine dropdown

    CONFIG_SCHEMA = [
        {"key": "server_url", "label": "Server URL:", "type": "text"},
        {"key": "voice_id",   "label": "Voice:",      "type": "voice_list",
         "fetch": "fetch_voices"},
        {"key": "rate",       "label": "Rate:",       "type": "slider",
         "min": 50, "max": 200, "scale": 100.0, "fmt": "scale_x", "default": 100},
        {"type": "note", "text": "Connect to a running synthesis server."},
    ]

    def __init__(self):
        self.server_url = ""
        self.voice_id   = ""
        self.rate       = 1.0

    def is_available(self) -> bool:
        return True   # replace with package availability check if needed

    def synthesize(self, text: str, sample_rate: int, channels: int):
        # TODO: connect to self.server_url, synthesize text, return float32 ndarray
        # Shape must be (channels, frames).  Return None on error.
        raise NotImplementedError

    @classmethod
    def fetch_voices(cls, config: dict) -> list[tuple[str, str]]:
        """Return [(voice_id, display_label), ...] for the current config."""
        server_url = config.get("server_url", "")
        # TODO: fetch voice list from server
        return [("voice1", "Voice 1"), ("voice2", "Voice 2")]

    def get_config(self) -> dict:
        return {"server_url": self.server_url,
                "voice_id":   self.voice_id,
                "rate":       self.rate}

    def set_config(self, cfg: dict):
        self.server_url = cfg.get("server_url", "")
        self.voice_id   = cfg.get("voice_id",   "")
        self.rate       = float(cfg.get("rate",  1.0))
