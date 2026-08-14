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
        "scp_bind_failed_title": "Local receiver could not start",
        "scp_bind_failed_msg": (
            "The local PACS for '{name}' did not respond, and the "
            "built-in receiver could not be started either "
            "(port {port} is unavailable).\n\nThe service was not "
            "started. Free the port or correct the local settings, "
            "then try again."),
        "copy": "Copy",
        "clear": "Clear",
        "slow_transfer_title": "Slow download detected",
        "slow_transfer_msg": (
            "A download from '{name}' appears to be stuck (no progress "
            "for some time).\n\nThe service is restarting "
            "automatically."),
        "pacs_retry_status": (
            "PACS not reachable \u2014 retrying in {seconds}s\u2026"),
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
        "scp_bind_failed_title": "Lokaler Empf\u00e4nger konnte nicht starten",
        "scp_bind_failed_msg": (
            "Das lokale PACS f\u00fcr '{name}' hat nicht geantwortet, und "
            "der integrierte Empf\u00e4nger konnte ebenfalls nicht "
            "gestartet werden (Port {port} ist belegt).\n\nDer Dienst "
            "wurde nicht gestartet. Bitte den Port freigeben oder die "
            "lokalen Einstellungen korrigieren und es erneut "
            "versuchen."),
        "copy": "Kopieren",
        "clear": "Leeren",
        "slow_transfer_title": "Langsamer Download erkannt",
        "slow_transfer_msg": (
            "Ein Download bei '{name}' scheint zu h\u00e4ngen (kein "
            "Fortschritt seit l\u00e4ngerer Zeit).\n\nDer Dienst wird "
            "automatisch neu gestartet."),
        "pacs_retry_status": (
            "PACS nicht erreichbar \u2014 neuer Versuch in "
            "{seconds}s\u2026"),
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
        "scp_bind_failed_title": "Le r\u00e9cepteur local n'a pas pu d\u00e9marrer",
        "scp_bind_failed_msg": (
            "Le PACS local pour '{name}' n'a pas r\u00e9pondu, et le "
            "r\u00e9cepteur int\u00e9gr\u00e9 n'a pas pu d\u00e9marrer non plus "
            "(le port {port} est occup\u00e9).\n\nLe service n'a pas "
            "\u00e9t\u00e9 d\u00e9marr\u00e9. Lib\u00e9rez le port ou corrigez les "
            "param\u00e8tres locaux, puis r\u00e9essayez."),
        "copy": "Copier",
        "clear": "Effacer",
        "slow_transfer_title": "T\u00e9l\u00e9chargement lent d\u00e9tect\u00e9",
        "slow_transfer_msg": (
            "Un t\u00e9l\u00e9chargement depuis '{name}' semble bloqu\u00e9 "
            "(aucune progression depuis un certain temps).\n\nLe service "
            "red\u00e9marre automatiquement."),
        "pacs_retry_status": (
            "PACS injoignable \u2014 nouvel essai dans "
            "{seconds}s\u2026"),
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
        "scp_bind_failed_title": "El receptor local no pudo iniciarse",
        "scp_bind_failed_msg": (
            "El PACS local de '{name}' no respondi\u00f3, y el receptor "
            "integrado tampoco pudo iniciarse (el puerto {port} no "
            "est\u00e1 disponible).\n\nEl servicio no se inici\u00f3. Libere "
            "el puerto o corrija la configuraci\u00f3n local y vuelva a "
            "intentarlo."),
        "copy": "Copiar",
        "clear": "Borrar",
        "slow_transfer_title": "Descarga lenta detectada",
        "slow_transfer_msg": (
            "Una descarga desde '{name}' parece bloqueada (sin progreso "
            "durante un tiempo).\n\nEl servicio se est\u00e1 reiniciando "
            "autom\u00e1ticamente."),
        "pacs_retry_status": (
            "PACS no accesible \u2014 reintentando en "
            "{seconds}s\u2026"),
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
