"""
Tests for core.institution_filter — the pure filter truth table.
"""

from core import institution_filter as f


def _evaluate(name, *, enabled=True, active=("A",), assignments=None):
    return f.evaluate(name, filtering_enabled=enabled,
                      active_groups=active,
                      assignments=assignments or {})


class TestFilteringDisabled:

    def test_everything_passes(self):
        v = _evaluate("Somewhere", enabled=False)
        assert v.allowed is True

    def test_unknown_is_not_reported_when_filtering_is_off(self):
        """Nothing to assign it to — the popup would be noise."""
        assert _evaluate("Somewhere", enabled=False).is_unknown is False


class TestNoActiveGroups:

    def test_treated_as_no_filtering(self):
        """Enabled but nothing selected is a half-finished config;
        blocking everything would silently stop all downloads."""
        assert _evaluate("Somewhere", active=()).allowed is True

    def test_unknown_not_reported(self):
        assert _evaluate("Somewhere", active=()).is_unknown is False


class TestAssignedInstitution:

    def test_active_group_passes(self):
        v = _evaluate("Clinic", active=("A",), assignments={"Clinic": "A"})
        assert (v.allowed, v.is_unknown) == (True, False)

    def test_inactive_group_is_skipped(self):
        v = _evaluate("Clinic", active=("A",), assignments={"Clinic": "B"})
        assert (v.allowed, v.is_unknown) == (False, False)


class TestUnassignedInstitution:

    def test_downloaded_and_reported(self):
        """Unknown means "download it AND ask the user where it
        belongs" — never "skip it"."""
        v = _evaluate("New Clinic", assignments={"Other": "A"})
        assert (v.allowed, v.is_unknown) == (True, True)

    def test_empty_assignment_counts_as_unassigned(self):
        v = _evaluate("Clinic", assignments={"Clinic": ""})
        assert (v.allowed, v.is_unknown) == (True, True)

    def test_empty_name_is_allowed_but_not_reported(self):
        """A PACS that omits InstitutionName gives the user nothing to
        assign, so the popup would be unactionable."""
        v = _evaluate("", assignments={})
        assert (v.allowed, v.is_unknown) == (True, False)


class TestVerdictShape:

    def test_verdict_is_immutable(self):
        import dataclasses
        import pytest
        v = _evaluate("X")
        with pytest.raises(dataclasses.FrozenInstanceError):
            v.allowed = False
