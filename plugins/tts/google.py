"""Google Cloud Text-to-Speech engine."""

from pubstreamer.tts.base import TtsEngine, decode_audio_bytes


class GoogleEngine(TtsEngine):
    name = "Google Cloud"
    key  = "google"

    CONFIG_SCHEMA = [
        {"key": "api_key",       "label": "API key:", "type": "text", "password": True},
        {"key": "language_code", "label": "Language code:", "type": "text"},
        {"key": "voice_name",    "label": "Voice:", "type": "voice_list",
         "fetch": "fetch_voices"},
        {"type": "note",
         "text": "Leave API key blank to use Application Default Credentials."},
    ]

    def __init__(self, api_key: str = "", voice_name: str = "en-US-Wavenet-C",
                 language_code: str = "en-US"):
        self.api_key       = api_key
        self.voice_name    = voice_name
        self.language_code = language_code

    def is_available(self) -> bool:
        try:
            from google.cloud import texttospeech  # noqa: F401
            return True
        except ImportError:
            return False

    def _make_client(self):
        from google.cloud import texttospeech
        if self.api_key:
            from google.api_core.client_options import ClientOptions
            return texttospeech.TextToSpeechClient(
                client_options=ClientOptions(api_key=self.api_key)
            )
        return texttospeech.TextToSpeechClient()   # Application Default Credentials

    def synthesize(self, text: str, sample_rate: int, channels: int):
        try:
            from google.cloud import texttospeech
            client = self._make_client()
            synthesis_input = texttospeech.SynthesisInput(text=text)
            voice = texttospeech.VoiceSelectionParams(
                language_code=self.language_code,
                name=self.voice_name,
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                sample_rate_hertz=16000,
            )
            resp = client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )
            return decode_audio_bytes(resp.audio_content, sample_rate, channels)
        except Exception as e:
            print(f"[Google TTS] synthesize error: {e}", flush=True)
            return None

    @classmethod
    def fetch_voices(cls, config: dict) -> list[tuple[str, str]]:
        api_key       = config.get("api_key", "")
        language_code = config.get("language_code", "")
        try:
            from google.cloud import texttospeech
            if api_key:
                from google.api_core.client_options import ClientOptions
                client = texttospeech.TextToSpeechClient(
                    client_options=ClientOptions(api_key=api_key)
                )
            else:
                client = texttospeech.TextToSpeechClient()
            resp = client.list_voices(language_code=language_code or "")
            return [(name, name) for name in sorted(v.name for v in resp.voices)]
        except Exception as e:
            raise RuntimeError(f"Google voice list failed: {e}") from e

    def get_config(self) -> dict:
        return {"api_key":       self.api_key,
                "voice_name":    self.voice_name,
                "language_code": self.language_code}

    def set_config(self, cfg: dict):
        self.api_key       = cfg.get("api_key",       "")
        self.voice_name    = cfg.get("voice_name",    "en-US-Wavenet-C")
        self.language_code = cfg.get("language_code", "en-US")
