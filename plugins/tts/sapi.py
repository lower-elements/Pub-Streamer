"""SAPI 5 TTS engine — renders speech to an in-memory stream via COM."""

from pubstreamer.tts.base import TtsEngine, pcm16_to_wav, decode_audio_bytes

# SPSTREAMFORMAT enum: 44.1 kHz 16-bit mono.
_SPSF_44kHz16BitMono = 34


class SapiEngine(TtsEngine):
    name = "SAPI 5"
    key  = "sapi"

    CONFIG_SCHEMA = [
        {"key": "voice_index", "label": "Voice:",   "type": "voice_list"},
        {"key": "rate",        "label": "Rate:",     "type": "slider",
         "min": -10, "max": 10, "default": 0},
        {"key": "volume",      "label": "Volume:",   "type": "slider",
         "min": 0, "max": 100, "default": 100},
    ]

    def __init__(self, voice_index: int = 0, rate: int = 0, volume: int = 100):
        self.voice_index = voice_index
        self.rate        = rate
        self.volume      = volume
        self._voices_cache: list[str] = []

    def is_available(self) -> bool:
        try:
            import win32com.client  # noqa: F401
            return True
        except ImportError:
            return False

    def list_voices(self) -> list[str]:
        if self._voices_cache:
            return self._voices_cache
        try:
            import pythoncom, win32com.client
            pythoncom.CoInitialize()
            try:
                v  = win32com.client.Dispatch("SAPI.SpVoice")
                vs = v.GetVoices()
                self._voices_cache = [vs.Item(i).GetDescription()
                                      for i in range(vs.Count)]
            finally:
                pythoncom.CoUninitialize()
        except Exception:
            pass
        return self._voices_cache

    def synthesize(self, text: str, sample_rate: int, channels: int):
        try:
            import pythoncom, win32com.client
            pythoncom.CoInitialize()
            try:
                voice  = win32com.client.Dispatch("SAPI.SpVoice")
                stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
                fmt    = win32com.client.Dispatch("SAPI.SpAudioFormat")
                fmt.Type = _SPSF_44kHz16BitMono
                stream.Format = fmt

                voices = voice.GetVoices()
                if 0 <= self.voice_index < voices.Count:
                    voice.Voice = voices.Item(self.voice_index)
                voice.Rate   = max(-10, min(10, self.rate))
                voice.Volume = max(0, min(100, self.volume))

                voice.AudioOutputStream = stream
                voice.Speak(text, 0)   # synchronous

                raw = bytes(stream.GetData())
                wfx = stream.Format.GetWaveFormatEx()
                sr_actual = wfx.SamplesPerSec
                ch_actual = wfx.Channels
            finally:
                pythoncom.CoUninitialize()

            wav = pcm16_to_wav(raw, sr_actual, ch_actual)
            return decode_audio_bytes(wav, sample_rate, channels)
        except Exception as e:
            print(f"[SAPI] synthesize error: {e}", flush=True)
            return None

    def get_config(self) -> dict:
        return {"voice_index": self.voice_index,
                "rate":        self.rate,
                "volume":      self.volume}

    def set_config(self, cfg: dict):
        self.voice_index = int(cfg.get("voice_index", 0))
        self.rate        = int(cfg.get("rate",        0))
        self.volume      = int(cfg.get("volume",    100))
