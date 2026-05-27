"""Google Translate TTS engine via gTTS (free, no API key required)."""

import io
from pubstreamer.tts.base import TtsEngine, decode_audio_bytes


class GttsEngine(TtsEngine):
    name = "Google Translate"
    key  = "gtts"

    # (code, display label) pairs shown in the language list.
    LANGUAGES: list[tuple[str, str]] = [
        ("af", "Afrikaans"),    ("ar", "Arabic"),          ("bg", "Bulgarian"),
        ("bn", "Bengali"),      ("bs", "Bosnian"),         ("ca", "Catalan"),
        ("cs", "Czech"),        ("cy", "Welsh"),            ("da", "Danish"),
        ("de", "German"),       ("el", "Greek"),            ("en", "English"),
        ("eo", "Esperanto"),    ("es", "Spanish"),          ("et", "Estonian"),
        ("fi", "Finnish"),      ("fr", "French"),           ("gu", "Gujarati"),
        ("hi", "Hindi"),        ("hr", "Croatian"),         ("hu", "Hungarian"),
        ("hy", "Armenian"),     ("id", "Indonesian"),       ("is", "Icelandic"),
        ("it", "Italian"),      ("ja", "Japanese"),         ("jw", "Javanese"),
        ("km", "Khmer"),        ("kn", "Kannada"),          ("ko", "Korean"),
        ("la", "Latin"),        ("lv", "Latvian"),          ("mk", "Macedonian"),
        ("ml", "Malayalam"),    ("mr", "Marathi"),          ("my", "Myanmar"),
        ("ne", "Nepali"),       ("nl", "Dutch"),            ("no", "Norwegian"),
        ("pl", "Polish"),       ("pt", "Portuguese"),       ("ro", "Romanian"),
        ("ru", "Russian"),      ("si", "Sinhala"),          ("sk", "Slovak"),
        ("sq", "Albanian"),     ("sr", "Serbian"),          ("su", "Sundanese"),
        ("sv", "Swedish"),      ("sw", "Swahili"),          ("ta", "Tamil"),
        ("te", "Telugu"),       ("th", "Thai"),             ("tl", "Filipino"),
        ("tr", "Turkish"),      ("uk", "Ukrainian"),        ("ur", "Urdu"),
        ("vi", "Vietnamese"),   ("zh-CN", "Chinese (Simplified)"),
        ("zh-TW", "Chinese (Traditional)"),
    ]

    CONFIG_SCHEMA = [
        {"key": "lang", "label": "Language:", "type": "voice_list",
         "choices": LANGUAGES},
        {"key": "slow", "label": "Slow speed", "type": "checkbox"},
    ]

    def __init__(self, lang: str = "en", slow: bool = False):
        self.lang = lang
        self.slow = slow

    def is_available(self) -> bool:
        try:
            import gtts  # noqa: F401
            return True
        except ImportError:
            return False

    def synthesize(self, text: str, sample_rate: int, channels: int):
        try:
            from gtts import gTTS
            tts = gTTS(text=text, lang=self.lang, slow=self.slow)
            buf = io.BytesIO()
            tts.write_to_fp(buf)
            buf.seek(0)
            return decode_audio_bytes(buf.read(), sample_rate, channels)
        except Exception as e:
            print(f"[Google Translate TTS] error: {e}", flush=True)
            return None

    def get_config(self) -> dict:
        return {"lang": self.lang, "slow": self.slow}

    def set_config(self, cfg: dict):
        self.lang = cfg.get("lang", "en")
        self.slow = bool(cfg.get("slow", False))
