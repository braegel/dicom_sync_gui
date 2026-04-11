"""
Tests for core.i18n — UI translation helper.

The app must present its UI in English, German, French and Spanish.
The language is chosen in the app settings.  A small lookup function
`tr(key, language)` returns the localized string for each UI element.
The most important user-facing string is the "image transfer completed"
clipboard text produced by the live-completions Copy button — it is
pasted into radiology reports.
"""

import pytest

from core.i18n import (
    SUPPORTED_LANGUAGES,
    DEFAULT_LANGUAGE,
    tr,
)


# ═══════════════════════════════════════════════════════════════════════════
# Supported languages
# ═══════════════════════════════════════════════════════════════════════════

class TestSupportedLanguages:

    def test_contains_english(self):
        assert "en" in SUPPORTED_LANGUAGES

    def test_contains_german(self):
        assert "de" in SUPPORTED_LANGUAGES

    def test_contains_french(self):
        assert "fr" in SUPPORTED_LANGUAGES

    def test_contains_spanish(self):
        assert "es" in SUPPORTED_LANGUAGES

    def test_no_unexpected_languages(self):
        assert set(SUPPORTED_LANGUAGES) == {"en", "de", "fr", "es"}

    def test_default_language_is_english(self):
        assert DEFAULT_LANGUAGE == "en"

    def test_default_language_is_in_supported(self):
        assert DEFAULT_LANGUAGE in SUPPORTED_LANGUAGES


# ═══════════════════════════════════════════════════════════════════════════
# tr() — translation lookup
# ═══════════════════════════════════════════════════════════════════════════

class TestTranslationLookup:
    """Translation keys required by the live-completions Copy button.
    This is the string the radiologist pastes into the report."""

    KEY = "image_transfer_completed"

    def test_english(self):
        assert tr(self.KEY, "en") == "Image transfer completed"

    def test_german(self):
        """German clipboard text must be 'Abschluss Bildübertragung'
        (the user pastes this into the 'Befund')."""
        assert tr(self.KEY, "de") == "Abschluss Bildübertragung"

    def test_french(self):
        assert tr(self.KEY, "fr") == "Transfert d'images terminé"

    def test_spanish(self):
        assert tr(self.KEY, "es") == "Transferencia de imágenes completada"

    def test_unknown_language_falls_back_to_english(self):
        assert tr(self.KEY, "xx") == "Image transfer completed"

    def test_empty_language_falls_back_to_english(self):
        assert tr(self.KEY, "") == "Image transfer completed"

    def test_none_language_falls_back_to_english(self):
        assert tr(self.KEY, None) == "Image transfer completed"

    def test_default_language_argument(self):
        """Called without a language → English."""
        assert tr(self.KEY) == "Image transfer completed"

    def test_unknown_key_returns_key_as_fallback(self):
        """A missing key must degrade gracefully — return the key
        itself rather than raising, so a typo never crashes the UI."""
        assert tr("this_key_does_not_exist", "en") == "this_key_does_not_exist"
        assert tr("this_key_does_not_exist", "de") == "this_key_does_not_exist"


class TestTranslationCoverage:
    """Every supported language must have an entry for every key that
    English defines, so no UI element ever shows the raw key."""

    def test_every_language_has_image_transfer_completed(self):
        for lang in SUPPORTED_LANGUAGES:
            result = tr("image_transfer_completed", lang)
            assert result != "image_transfer_completed", (
                f"language {lang!r} is missing the "
                f"'image_transfer_completed' translation")
            assert len(result) > 0
