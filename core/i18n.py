"""
UI translation helper for DICOM Sync GUI.

Supported languages: English (en), German (de), French (fr), Spanish (es).
The user picks the language in Settings; `tr(key, language)` returns the
localized string for a UI element.  Missing keys or unknown languages
fall back to English so the UI never shows raw keys.
"""

from typing import Dict, Optional


SUPPORTED_LANGUAGES = ["en", "de", "fr", "es"]
DEFAULT_LANGUAGE = "en"


_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "en": {
        "image_transfer_completed": "Image transfer completed",
    },
    "de": {
        "image_transfer_completed": "Abschluss Bildübertragung",
    },
    "fr": {
        "image_transfer_completed": "Transfert d'images terminé",
    },
    "es": {
        "image_transfer_completed": "Transferencia de imágenes completada",
    },
}


def tr(key: str, language: Optional[str] = DEFAULT_LANGUAGE) -> str:
    """Return the translation for *key* in *language*.

    Falls back to English if the language is unknown/empty/None.
    Falls back to *key* itself if neither the requested language nor
    English has an entry for the key.
    """
    lang = language if language in _TRANSLATIONS else DEFAULT_LANGUAGE
    table = _TRANSLATIONS.get(lang, {})
    if key in table:
        return table[key]
    en = _TRANSLATIONS.get(DEFAULT_LANGUAGE, {})
    return en.get(key, key)
