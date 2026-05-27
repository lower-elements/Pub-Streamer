"""Lightweight gettext wrapper — call setup() before any UI is created."""

import gettext
import locale
import pathlib

_LOCALE_DIR = pathlib.Path(__file__).parent.parent / "locale"
_DOMAIN = "pubstreamer"

_current: gettext.NullTranslations = gettext.NullTranslations()


def _(msgid: str) -> str:
    return _current.gettext(msgid)


def setup(language: str = "") -> None:
    """Install translations.

    language is a BCP-47/ISO-639-1 tag ('ja', 'en', …) or '' for system default.
    English (and unknown languages) fall through to NullTranslations, which
    returns the msgid unchanged — no .mo file needed for English.
    """
    global _current
    if not language:
        try:
            lang, _ = locale.getdefaultlocale()
            language = (lang or "").split("_")[0]
        except Exception:
            language = "en"
    if language in ("en", ""):
        _current = gettext.NullTranslations()
        return
    try:
        _current = gettext.translation(
            _DOMAIN, localedir=str(_LOCALE_DIR), languages=[language]
        )
    except FileNotFoundError:
        _current = gettext.NullTranslations()
