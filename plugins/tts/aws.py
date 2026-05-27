"""AWS Polly TTS engine."""

from pubstreamer.tts.base import TtsEngine, pcm16_to_wav, decode_audio_bytes


class AwsEngine(TtsEngine):
    name = "AWS Polly"
    key  = "aws"

    CONFIG_SCHEMA = [
        {"key": "access_key_id",     "label": "Access key ID:", "type": "text"},
        {"key": "secret_access_key", "label": "Secret access key:", "type": "text",
         "password": True},
        {"key": "region",            "label": "Region (e.g. us-east-1):", "type": "text"},
        {"key": "engine",            "label": "Engine:", "type": "choice",
         "choices": ["neural", "standard"]},
        {"key": "voice_id",          "label": "Voice ID:", "type": "voice_list",
         "fetch": "fetch_voices"},
    ]

    def __init__(self, access_key_id: str = "", secret_access_key: str = "",
                 region: str = "us-east-1", voice_id: str = "Joanna",
                 engine: str = "neural"):
        self.access_key_id     = access_key_id
        self.secret_access_key = secret_access_key
        self.region            = region
        self.voice_id          = voice_id
        self.engine            = engine    # "standard" or "neural"

    def is_available(self) -> bool:
        try:
            import boto3  # noqa: F401
            return True
        except ImportError:
            return False

    def synthesize(self, text: str, sample_rate: int, channels: int):
        if not self.access_key_id or not self.secret_access_key:
            print("[AWS Polly] credentials not configured", flush=True)
            return None
        try:
            import boto3
            polly = boto3.client(
                "polly",
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
            )
            resp = polly.synthesize_speech(
                Text=text,
                OutputFormat="pcm",   # 16-bit LE mono at 16 kHz
                VoiceId=self.voice_id,
                Engine=self.engine,
            )
            pcm = resp["AudioStream"].read()
            wav = pcm16_to_wav(pcm, 16000, 1)
            return decode_audio_bytes(wav, sample_rate, channels)
        except Exception as e:
            print(f"[AWS Polly] synthesize error: {e}", flush=True)
            return None

    @classmethod
    def fetch_voices(cls, config: dict) -> list[tuple[str, str]]:
        access_key_id     = config.get("access_key_id", "")
        secret_access_key = config.get("secret_access_key", "")
        region            = config.get("region", "us-east-1")
        engine            = config.get("engine", "neural")
        try:
            import boto3
            polly = boto3.client(
                "polly",
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name=region,
            )
            resp = polly.describe_voices(Engine=engine)
            return sorted([(v["Id"], v["Id"]) for v in resp.get("Voices", [])],
                          key=lambda x: x[0])
        except Exception as e:
            raise RuntimeError(f"AWS Polly voice list failed: {e}") from e

    def get_config(self) -> dict:
        return {"access_key_id":     self.access_key_id,
                "secret_access_key": self.secret_access_key,
                "region":            self.region,
                "voice_id":          self.voice_id,
                "engine":            self.engine}

    def set_config(self, cfg: dict):
        self.access_key_id     = cfg.get("access_key_id",     "")
        self.secret_access_key = cfg.get("secret_access_key", "")
        self.region            = cfg.get("region",            "us-east-1")
        self.voice_id          = cfg.get("voice_id",          "Joanna")
        self.engine            = cfg.get("engine",            "neural")
