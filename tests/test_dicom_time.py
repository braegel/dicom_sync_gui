"""
Tests for core.dicom_time — DICOM date/time parsing and display formats.

This module consolidated four hand-rolled implementations (the queue
table, the completions window, the examination lookup, and a dead pair
in core.dicom_ops).  The cases below carry over the behaviour each of
them relied on, so the consolidation cannot quietly change any of them.
"""

import pytest

from core.dicom_time import (
    format_date, format_date_time, format_duration, format_hhmm,
    format_hhmmss, parse_time,
)


class TestParseTime:
    """Accepts both the on-the-wire form and the already-formatted one:
    values reach this from a PACS response AND from widget text."""

    @pytest.mark.parametrize("value,expected", [
        ("143052", (14, 30, 52)),
        ("14:30:52", (14, 30, 52)),
        ("0900", (9, 0, 0)),
        ("09:00", (9, 0, 0)),
        ("000000", (0, 0, 0)),
    ])
    def test_accepted_forms(self, value, expected):
        assert parse_time(value) == expected

    @pytest.mark.parametrize("value", ["", "12", "abc", "  ", None])
    def test_unparseable_returns_none(self, value):
        assert parse_time(value) is None

    def test_never_raises_on_junk(self):
        assert parse_time("::::") is None
        assert parse_time("xx:yy:zz") is None

    def test_whitespace_is_tolerated(self):
        assert parse_time("  143052  ") == (14, 30, 52)


class TestFormatDate:

    def test_normal_date(self):
        assert format_date("20260308") == "08.03.2026"

    def test_new_year_and_end_of_year(self):
        assert format_date("20260101") == "01.01.2026"
        assert format_date("20261231") == "31.12.2026"

    @pytest.mark.parametrize("value", ["", None])
    def test_empty_in_empty_out(self, value):
        assert format_date(value) == ""

    @pytest.mark.parametrize("value", ["2026", "202603080", "2026-03-08"])
    def test_malformed_passed_through_verbatim(self, value):
        """A PACS that sends a partial date is better shown as-is than
        silently blanked."""
        assert format_date(value) == value


class TestFormatHhmm:

    def test_full_time(self):
        assert format_hhmm("143052") == "14:30"

    def test_four_digits(self):
        assert format_hhmm("0900") == "09:00"

    @pytest.mark.parametrize("value", ["", None, "12", "ab:cd"])
    def test_too_short_or_non_numeric_is_empty(self, value):
        assert format_hhmm(value) == ""


class TestFormatHhmmss:

    def test_full_time(self):
        assert format_hhmmss("143052") == "14:30:52"

    def test_fractional_seconds_are_ignored(self):
        assert format_hhmmss("143052.123") == "14:30:52"

    @pytest.mark.parametrize("value", ["", None])
    def test_empty_in_empty_out(self, value):
        assert format_hhmmss(value) == ""

    def test_short_value_passed_through(self):
        assert format_hhmmss("0900") == "0900"


class TestFormatDateTime:

    def test_both_parts(self):
        assert format_date_time("20260401", "081500") == "01.04.2026 08:15"

    def test_date_only(self):
        assert format_date_time("20260401", "") == "01.04.2026"

    def test_time_only(self):
        assert format_date_time("", "081500") == "08:15"

    def test_neither_returns_the_empty_marker(self):
        assert format_date_time("", "") == ""
        assert format_date_time("", "", empty="—") == "—"


class TestFormatDuration:
    """Minutes are deliberately NOT zero-padded in the short form — that
    is the shape the ETE column and the countdown have always used."""

    @pytest.mark.parametrize("seconds,expected", [
        (0, "0:00"),
        (5, "0:05"),
        (90, "1:30"),
        (545, "9:05"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3661, "1:01:01"),
        (86399, "23:59:59"),
    ])
    def test_formats(self, seconds, expected):
        assert format_duration(seconds) == expected

    def test_fractional_seconds_truncate(self):
        assert format_duration(90.9) == "1:30"

    def test_negative_treated_as_zero(self):
        assert format_duration(-10) == "0:00"
