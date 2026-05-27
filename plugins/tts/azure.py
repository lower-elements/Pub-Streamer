"""Azure Cognitive Services TTS engine."""

from pubstreamer.tts.base import TtsEngine, decode_audio_bytes


class AzureEngine(TtsEngine):
    name = "Azure"

    CONFIG_SCHEMA = [
        {"key": "subscription_key", "label": "Subscription key:", "type": "text",
         "password": True},
        {"key": "region",           "label": "Region (e.g. eastus):", "type": "text"},
        {"key": "voice_name",       "label": "Voice:", "type": "voice_list",
         "fetch": "fetch_voices"},
    ]

    def __init__(self, subscription_key: str = "", region: str = "",
                 voice_name: str = "en-US-JennyNeural"):
        self.subscription_key = subscription_key
        self.region           = region
        self.voice_name       = voice_name

    def is_available(self) -> bool:
        try:
            import azure.cognitiveservices.speech  # noqa: F401
            return True
        except ImportError:
            return False

    def synthesize(self, text: str, sample_rate: int, channels: int):
        if not self.subscription_key or not self.region:
            print("[Azure] subscription key or region not configured", flush=True)
            return None
        try:
            import azure.cognitiveservices.speech as sdk
            cfg = sdk.SpeechConfig(subscription=self.subscription_key,
                                   region=self.region)
            cfg.set_speech_synthesis_output_format(
                sdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm
            )
            if self.voice_name:
                cfg.speech_synthesis_voice_name = self.voice_name
            synth  = sdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
            result = synth.speak_text_async(text).get()
            if result.reason != sdk.ResultReason.SynthesizingAudioCompleted:
                print(f"[Azure] synthesis failed: {result.reason}", flush=True)
                return None
            return decode_audio_bytes(result.audio_data, sample_rate, channels)
        except Exception as e:
            print(f"[Azure] synthesize error: {e}", flush=True)
            return None

    @classmethod
    def fetch_voices(cls, config: dict) -> list[tuple[str, str]]:
        subscription_key = config.get("subscription_key", "")
        region           = config.get("region", "")
        try:
            import azure.cognitiveservices.speech as sdk
            cfg   = sdk.SpeechConfig(subscription=subscription_key, region=region)
            synth = sdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
            result = synth.get_voices_async().get()
            return [(name, name)
                    for name in sorted(v.short_name for v in (result.voices or []))]
        except Exception as e:
            raise RuntimeError(f"Azure voice list failed: {e}") from e

    def get_config(self) -> dict:
        return {"subscription_key": self.subscription_key,
                "region":           self.region,
                "voice_name":       self.voice_name}

    def set_config(self, cfg: dict):
        self.subscription_key = cfg.get("subscription_key", "")
        self.region           = cfg.get("region",           "")
        self.voice_name       = cfg.get("voice_name",       "en-US-JennyNeural")
