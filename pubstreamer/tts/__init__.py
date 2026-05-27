"""TTS engine registry — maps display names to engine classes."""

from .base        import TtsEngine
from .sapi        import SapiEngine
from .piper       import PiperEngine
from .star        import StarEngine
from .azure       import AzureEngine
from .google      import GoogleEngine
from .aws         import AwsEngine
from .gtts        import GttsEngine
from .elevenlabs  import ElevenLabsEngine
from .openai_tts  import OpenAITtsEngine
from .edge        import EdgeEngine

# Display names in the order they appear in the UI combobox.
ENGINE_NAMES: list[str] = [
    "SAPI 5",
    "Piper",
    "Star",
    "ElevenLabs",
    "OpenAI",
    "Azure",
    "Google Cloud",
    "Google Translate",
    "AWS Polly",
    "Edge TTS",
]

# Internal keys saved to config (stable, never rename).
_ENGINE_KEY: dict[str, str] = {
    "SAPI 5":           "sapi",
    "Piper":            "piper",
    "Star":             "star",
    "ElevenLabs":       "elevenlabs",
    "OpenAI":           "openai",
    "Azure":            "azure",
    "Google Cloud":     "google",
    "Google Translate": "gtts",
    "AWS Polly":        "aws",
    "Edge TTS":         "edge",
}

_ENGINE_CLASS: dict[str, type] = {
    "sapi":       SapiEngine,
    "piper":      PiperEngine,
    "star":       StarEngine,
    "elevenlabs": ElevenLabsEngine,
    "openai":     OpenAITtsEngine,
    "azure":      AzureEngine,
    "google":     GoogleEngine,
    "gtts":       GttsEngine,
    "aws":        AwsEngine,
    "edge":       EdgeEngine,
}

_KEY_FOR_NAME  = _ENGINE_KEY
_NAME_FOR_KEY  = {v: k for k, v in _ENGINE_KEY.items()}


def engine_key(display_name: str) -> str:
    return _ENGINE_KEY.get(display_name, "sapi")


def engine_display_name(key: str) -> str:
    return _NAME_FOR_KEY.get(key, "SAPI 5")


def make_engine(key: str, cfg: dict | None = None) -> TtsEngine:
    """Construct an engine by its config key and restore its config dict."""
    cls = _ENGINE_CLASS.get(key, SapiEngine)
    eng = cls()
    if cfg:
        eng.set_config(cfg)
    return eng
