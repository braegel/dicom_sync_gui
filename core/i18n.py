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
        "pacs_unreachable_title": "PACS not reachable",
        "pacs_unreachable_msg": (
            "The source PACS '{name}' ({ip}:{port}) did not respond to "
            "the connection test.\n\nThe service was not started. "
            "Please check that the PACS is online and the network "
            "settings are correct."),
        "pacs_connection_lost_title": "PACS connection lost",
        "pacs_connection_lost_msg": (
            "The connection to PACS '{name}' was lost during an active "
            "transfer.\n\nThe service keeps retrying and will resume "
            "automatically once the PACS is reachable again."),
    },
    "de": {
        "image_transfer_completed": "Abschluss Bildeingang",
        "pacs_unreachable_title": "PACS nicht erreichbar",
        "pacs_unreachable_msg": (
            "Das Quell-PACS '{name}' ({ip}:{port}) hat nicht auf den "
            "Verbindungstest geantwortet.\n\nDer Dienst wurde nicht "
            "gestartet. Bitte prüfen Sie, ob das PACS online ist und "
            "die Netzwerkeinstellungen korrekt sind."),
        "pacs_connection_lost_title": "PACS-Verbindung verloren",
        "pacs_connection_lost_msg": (
            "Die Verbindung zum PACS '{name}' ist während einer "
            "laufenden Übertragung abgebrochen.\n\nDer Dienst versucht "
            "es weiter und nimmt die Übertragung automatisch wieder "
            "auf, sobald das PACS wieder erreichbar ist."),
    },
    "fr": {
        "image_transfer_completed": "Transfert d'images terminé",
        "pacs_unreachable_title": "PACS injoignable",
        "pacs_unreachable_msg": (
            "Le PACS source '{name}' ({ip}:{port}) n'a pas répondu au "
            "test de connexion.\n\nLe service n'a pas été démarré. "
            "Veuillez vérifier que le PACS est en ligne et que les "
            "paramètres réseau sont corrects."),
        "pacs_connection_lost_title": "Connexion PACS perdue",
        "pacs_connection_lost_msg": (
            "La connexion au PACS '{name}' a été perdue pendant un "
            "transfert en cours.\n\nLe service continue d'essayer et "
            "reprendra automatiquement dès que le PACS sera de nouveau "
            "joignable."),
    },
    "es": {
        "image_transfer_completed": "Transferencia de imágenes completada",
        "pacs_unreachable_title": "PACS no accesible",
        "pacs_unreachable_msg": (
            "El PACS de origen '{name}' ({ip}:{port}) no respondió a la "
            "prueba de conexión.\n\nEl servicio no se inició. "
            "Compruebe que el PACS esté en línea y que la configuración "
            "de red sea correcta."),
        "pacs_connection_lost_title": "Conexión PACS perdida",
        "pacs_connection_lost_msg": (
            "Se perdió la conexión con el PACS '{name}' durante una "
            "transferencia activa.\n\nEl servicio sigue reintentando y "
            "se reanudará automáticamente cuando el PACS vuelva a estar "
            "accesible."),
    },
}


def tr(key: str, language: Optional[str] = DEFAULT_LANGUAGE,
       **kwargs: object) -> str:
    """Return the translation for *key* in *language*.

    Falls back to English if the language is unknown/empty/None.
    Falls back to *key* itself if neither the requested language nor
    English has an entry for the key.

    Any *kwargs* are substituted into the resolved string via
    ``str.format`` (e.g. ``tr("pacs_unreachable_msg", lang,
    name="CT", ip="1.2.3.4", port=104)``).  A missing placeholder
    leaves the template unformatted rather than raising.
    """
    lang = language if language in _TRANSLATIONS else DEFAULT_LANGUAGE
    table = _TRANSLATIONS.get(lang, {})
    en = _TRANSLATIONS.get(DEFAULT_LANGUAGE, {})
    text = table.get(key) or en.get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text
