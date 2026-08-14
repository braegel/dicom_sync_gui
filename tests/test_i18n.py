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
        """German clipboard text must be 'Abschluss Bildeingang'
        (the user pastes this into the 'Befund')."""
        assert tr(self.KEY, "de") == "Abschluss Bildeingang"

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

    def test_every_language_has_pacs_unreachable_strings(self):
        for lang in SUPPORTED_LANGUAGES:
            for key in ("pacs_unreachable_title", "pacs_unreachable_msg"):
                result = tr(key, lang)
                assert result != key and len(result) > 0, (
                    f"language {lang!r} is missing the {key!r} "
                    f"translation")

    def test_every_language_has_pacs_connection_lost_strings(self):
        for lang in SUPPORTED_LANGUAGES:
            for key in ("pacs_connection_lost_title",
                        "pacs_connection_lost_msg"):
                result = tr(key, lang)
                assert result != key and len(result) > 0, (
                    f"language {lang!r} is missing the {key!r} "
                    f"translation")


class TestTranslationFormatting:
    """``tr`` substitutes keyword placeholders so the PACS-unreachable
    message can name the offending source."""

    def test_placeholders_substituted(self):
        msg = tr("pacs_unreachable_msg", "de",
                 name="CT", ip="10.0.0.5", port=104)
        assert "CT" in msg and "10.0.0.5" in msg and "104" in msg
        assert "{" not in msg

    def test_english_placeholders_substituted(self):
        msg = tr("pacs_unreachable_msg", "en",
                 name="MRI", ip="1.2.3.4", port=11112)
        assert "MRI" in msg and "1.2.3.4" in msg and "11112" in msg

    def test_missing_placeholder_does_not_raise(self):
        # No kwargs supplied for a template with placeholders → return
        # the template unformatted rather than raising.
        msg = tr("pacs_unreachable_msg", "en")
        assert "{name}" in msg

    def test_formatting_ignored_for_plain_keys(self):
        # Extra kwargs on a placeholder-free string are harmless.
        assert tr("image_transfer_completed", "en",
                  unused="x") == "Image transfer completed"


class TestSharedUIKeys:
    """Keys shared by the dashboard, the main window and the
    completions window.  Every supported language must define them —
    these replaced hardcoded German literals, so a missing entry would
    reintroduce the exact bug they were added to fix."""

    def test_every_language_has_the_shared_ui_keys(self):
        for lang in SUPPORTED_LANGUAGES:
            for key in ("copy", "clear", "slow_transfer_title",
                        "slow_transfer_msg", "pacs_retry_status"):
                result = tr(key, lang)
                assert result != key and len(result) > 0, (
                    f"language {lang!r} is missing the {key!r} "
                    f"translation")

    def test_slow_transfer_msg_substitutes_the_source_name(self):
        for lang in SUPPORTED_LANGUAGES:
            msg = tr("slow_transfer_msg", lang, name="ct")
            assert "ct" in msg and "{" not in msg

    def test_pacs_retry_status_substitutes_the_delay(self):
        for lang in SUPPORTED_LANGUAGES:
            msg = tr("pacs_retry_status", lang, seconds=5)
            assert "5" in msg and "{" not in msg
