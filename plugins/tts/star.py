"""Star TTS coagulator client — synthesises via a running Star WebSocket server."""

import json
import struct

from pubstreamer.tts.base import TtsEngine, decode_audio_bytes


class StarEngine(TtsEngine):
    name = "Star"

    CONFIG_SCHEMA = [
        {"key": "host",  "label": "Server URL:",  "type": "text"},
        {"key": "voice", "label": "Voice name:",  "type": "text"},
    ]

    def __init__(self, host: str = "ws://localhost:4567", voice: str = ""):
        self.host  = host
        self.voice = voice

    def is_available(self) -> bool:
        try:
            import websockets  # noqa: F401
            return True
        except ImportError:
            return False

    def synthesize(self, text: str, sample_rate: int, channels: int):
        if not self.host:
            print("[Star] no host configured", flush=True)
            return None
        try:
            import websockets.sync.client
            request_str = f"{self.voice}: {text}" if self.voice else text
            payload = json.dumps({"user": 4, "request": [request_str]})

            with websockets.sync.client.connect(
                self.host, max_size=None, open_timeout=10
            ) as ws:
                ws.send(payload)
                raw = ws.recv()

            if isinstance(raw, str):
                print(f"[Star] server error: {raw}", flush=True)
                return None

            data = bytes(raw)
            # Binary protocol: 2-byte LE metadata-JSON length, then JSON, then audio
            meta_len = struct.unpack_from("<H", data, 0)[0]
            meta     = json.loads(data[2:2 + meta_len].decode("utf-8"))
            audio    = data[2 + meta_len:]
            # 'extension' tells us the format (wav, mp3, ogg, opus…)
            # pedalboard decodes all of them transparently.
            _ = meta.get("extension", "wav")
            return decode_audio_bytes(audio, sample_rate, channels)
        except Exception as e:
            print(f"[Star] synthesize error: {e}", flush=True)
            return None

    def list_voices(self) -> list[str]:
        """Fetch the voice list from the running coagulator."""
        try:
            import websockets.sync.client
            payload = json.dumps({"user": 4})
            with websockets.sync.client.connect(
                self.host, max_size=None, open_timeout=5
            ) as ws:
                ws.send(payload)
                msg = ws.recv()
            data = json.loads(msg) if isinstance(msg, str) else {}
            return data.get("voices", [])
        except Exception:
            return []

    def get_config(self) -> dict:
        return {"host": self.host, "voice": self.voice}

    def set_config(self, cfg: dict):
        self.host  = cfg.get("host",  "ws://localhost:4567")
        self.voice = cfg.get("voice", "")
