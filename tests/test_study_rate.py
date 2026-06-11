"""
Tests for gui.study_rate — the pure studies-per-hour helpers.

The widget-level behaviour (label updates, high-load popup) is covered
in tests/test_dashboard.py; here we exercise the extracted pure
functions directly, without Qt or an AppConfig.
"""

from datetime import datetime, timedelta

from gui.study_rate import (
    COLOR_GREEN, COLOR_RED, COLOR_YELLOW,
    compute_study_rates, study_rate_color,
)

ASSIGNMENTS = {
    "Hospital Alpha": "Group A",
    "Clinic Beta": "Group B",
}


def _study(uid: str, institution: str, minutes_ago: int,
           now: datetime) -> dict:
    """Study-level dict shaped like a raw query result."""
    dt = now - timedelta(minutes=minutes_ago)
    return {
        "study_uid": uid,
        "study_date": dt.strftime("%Y%m%d"),
        "study_time": dt.strftime("%H%M%S"),
        "institution_name": institution,
    }


class TestComputeStudyRates:

    def setup_method(self):
        self.now = datetime.now()

    def _rates(self, studies, *, enabled=True):
        return compute_study_rates(
            studies,
            filter_groups_enabled=enabled,
            institution_assignments=ASSIGNMENTS,
            now=self.now)

    def test_grouped_by_filter_group_when_enabled(self):
        rates = self._rates([
            _study("S1", "Hospital Alpha", 5, self.now),
            _study("S2", "Clinic Beta", 5, self.now),
        ])
        assert rates == {"Group A": 1, "Group B": 1}

    def test_pooled_under_total_when_disabled(self):
        rates = self._rates([
            _study("S1", "Hospital Alpha", 5, self.now),
            _study("S2", "Clinic Beta", 5, self.now),
        ], enabled=False)
        assert rates == {"_total": 2}

    def test_unassigned_institution_grouped_under_empty_key(self):
        rates = self._rates([_study("S1", "Unknown Clinic", 5, self.now)])
        assert rates == {"": 1}

    def test_60_minute_cutoff_is_exclusive(self):
        # Exactly 60 minutes old → excluded; 59 minutes → included.
        rates = self._rates([
            _study("OLD", "Hospital Alpha", 60, self.now),
            _study("NEW", "Hospital Alpha", 59, self.now),
        ])
        assert rates == {"Group A": 1}

    def test_duplicate_study_uids_count_once(self):
        rates = self._rates([
            _study("S1", "Hospital Alpha", 5, self.now),
            _study("S1", "Hospital Alpha", 10, self.now),
        ])
        assert rates == {"Group A": 1}

    def test_unparseable_or_missing_date_skipped(self):
        bad = _study("S1", "Hospital Alpha", 5, self.now)
        bad["study_date"] = "not-a-date"
        missing = _study("S2", "Hospital Alpha", 5, self.now)
        missing["study_date"] = ""
        assert self._rates([bad, missing]) == {}


class TestStudyRateColor:

    def test_zero_is_neutral(self):
        assert study_rate_color(0) is None

    def test_bands(self):
        assert study_rate_color(1) == COLOR_GREEN
        assert study_rate_color(5) == COLOR_GREEN
        assert study_rate_color(6) == COLOR_YELLOW
        assert study_rate_color(11) == COLOR_YELLOW
        assert study_rate_color(12) == COLOR_RED
