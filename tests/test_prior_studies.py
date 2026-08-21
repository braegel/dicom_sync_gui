"""
Tests for core.prior_studies — the pure prior-selection rules.
"""

import pytest
from pydicom.dataset import Dataset

from core import prior_studies

# The fixtures below use short, readable stand-ins ("ct1", "mid") where
# real data carries dotted UIDs, and deliberately malformed modality
# strings to pin down what we do with a non-conformant PACS.  pydicom
# rightly warns about both; the warnings are about the fixtures, not
# about anything under test.
pytestmark = pytest.mark.filterwarnings(
    "ignore:Invalid value for VR:UserWarning")


def _study(*, uid="1", pid="P1", date="20260101", time="120000",
           modalities=None):
    ds = Dataset()
    ds.StudyInstanceUID = uid
    ds.PatientID = pid
    ds.StudyDate = date
    ds.StudyTime = time
    if modalities is not None:
        ds.ModalitiesInStudy = modalities
    return ds


class TestSplitModalities:

    def test_multivalue_from_a_conformant_pacs(self):
        """A conformant ``CT\\MR`` becomes a pydicom MultiValue once
        parsed.  ``str()`` on it yields "['CT', 'MR']" — the old
        stringify-and-split produced the tokens ``['CT'`` and ``'MR']``,
        so a multi-modality study matched nothing and the filter
        discarded every prior."""
        assert prior_studies.split_modalities(
            _study(modalities="CT\\MR")) == {"CT", "MR"}

    def test_multivalue_passed_as_a_list(self):
        assert prior_studies.split_modalities(
            _study(modalities=["CT", "MR"])) == {"CT", "MR"}

    def test_single_value_string(self):
        assert prior_studies.split_modalities(
            _study(modalities="CT")) == {"CT"}

    def test_none_is_empty_set(self):
        ds = _study()
        ds.ModalitiesInStudy = None
        assert prior_studies.split_modalities(ds) == set()

    def test_comma_separated(self):
        """Several PACS send comma-separated multi-values.  pydicom
        rightly warns that it is not a valid CS — the point of the test
        is that we still cope with what such a PACS sends."""
        assert prior_studies.split_modalities(
            _study(modalities="CT,MR")) == {"CT", "MR"}

    def test_whitespace_and_empty_segments_dropped(self):
        assert prior_studies.split_modalities(
            _study(modalities=" CT ,, MR ")) == {"CT", "MR"}

    def test_missing_tag_is_empty_set(self):
        assert prior_studies.split_modalities(_study()) == set()


class TestSortNewestFirst:

    def test_orders_by_date_then_time_descending(self):
        a = _study(uid="a", date="20260101", time="090000")
        b = _study(uid="b", date="20260101", time="180000")
        c = _study(uid="c", date="20260102", time="010000")
        out = prior_studies.sort_newest_first([a, b, c])
        assert [s.StudyInstanceUID for s in out] == ["c", "b", "a"]

    def test_input_is_not_mutated(self):
        a = _study(uid="a", date="20260101")
        b = _study(uid="b", date="20260102")
        original = [a, b]
        prior_studies.sort_newest_first(original)
        assert [s.StudyInstanceUID for s in original] == ["a", "b"]


class TestFilterByModality:

    def test_keeps_only_intersecting_studies(self):
        priors = [_study(uid="ct", modalities="CT"),
                  _study(uid="mr", modalities="MR")]
        current = [_study(uid="now", modalities="CT")]
        kept = prior_studies.filter_by_modality(priors, current, "P1")
        assert [s.StudyInstanceUID for s in kept] == ["ct"]

    def test_only_the_named_patients_current_studies_set_the_target(self):
        priors = [_study(uid="mr", modalities="MR")]
        current = [_study(uid="other", pid="P2", modalities="MR")]
        # P1 has no current study -> no target -> nothing to filter on
        assert prior_studies.filter_by_modality(
            priors, current, "P1") == priors

    def test_no_modality_information_keeps_everything(self):
        """A PACS that omits ModalitiesInStudy must not silently switch
        the feature off by dropping every prior."""
        priors = [_study(uid="a"), _study(uid="b")]
        current = [_study(uid="now")]
        assert prior_studies.filter_by_modality(
            priors, current, "P1") == priors

    def test_multi_modality_prior_matches_on_any_overlap(self):
        priors = [_study(uid="mix", modalities="MR\\CT")]
        current = [_study(uid="now", modalities="CT")]
        assert len(prior_studies.filter_by_modality(
            priors, current, "P1")) == 1


class TestSelectPriors:

    def _three(self):
        return [_study(uid="old", date="20250101"),
                _study(uid="mid", date="20260101"),
                _study(uid="new", date="20260201")]

    def test_newest_first_and_truncated(self):
        out = prior_studies.select_priors(
            self._three(), [], "P1", same_modality=False, count=2)
        assert [s.StudyInstanceUID for s in out] == ["new", "mid"]

    def test_count_zero_returns_nothing(self):
        assert prior_studies.select_priors(
            self._three(), [], "P1", same_modality=False, count=0) == []

    def test_negative_count_returns_nothing(self):
        """``[:-1]`` would quietly return all but the newest instead."""
        assert prior_studies.select_priors(
            self._three(), [], "P1", same_modality=False, count=-1) == []

    def test_count_above_available_returns_all(self):
        out = prior_studies.select_priors(
            self._three(), [], "P1", same_modality=False, count=99)
        assert len(out) == 3

    def test_modality_filter_applies_before_the_cap(self):
        """Otherwise the cap could be spent on studies the filter then
        removes, returning fewer priors than configured."""
        priors = [_study(uid="mr1", date="20260301", modalities="MR"),
                  _study(uid="mr2", date="20260228", modalities="MR"),
                  _study(uid="ct1", date="20260101", modalities="CT")]
        current = [_study(uid="now", modalities="CT")]
        out = prior_studies.select_priors(
            priors, current, "P1", same_modality=True, count=1)
        assert [s.StudyInstanceUID for s in out] == ["ct1"]

    def test_log_callback_receives_the_trail(self):
        lines = []
        prior_studies.select_priors(
            self._three(), [], "P1", same_modality=False, count=1,
            log=lines.append)
        assert any("downloading 1 of 3" in ln for ln in lines)

    def test_log_is_optional(self):
        prior_studies.select_priors(
            self._three(), [], "P1", same_modality=True, count=1)
