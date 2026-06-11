"""
Tests for core.stats_utils — the shared median / pstdev / quartile
helpers that replaced six hand-rolled implementations across
transfer_engine, transfer_log, live_completions and
transfer_stats_window.
"""

import math

import pytest

from core.stats_utils import median, median_and_pstdev, tukey_quartiles


# ═══════════════════════════════════════════════════════════════════════════
# median
# ═══════════════════════════════════════════════════════════════════════════

class TestMedian:

    def test_empty_returns_zero(self):
        """Empty input → 0.0 sentinel (statistics.median would raise)."""
        assert median([]) == 0.0

    def test_single_value(self):
        assert median([5.0]) == 5.0

    def test_odd_count_middle_element(self):
        assert median([1.0, 2.0, 3.0]) == 2.0

    def test_even_count_averages_middle_pair(self):
        assert median([1.0, 2.0, 3.0, 4.0]) == 2.5

    def test_even_pair(self):
        assert median([1.0, 3.0]) == 2.0

    def test_unsorted_input(self):
        assert median([3.0, 1.0, 2.0]) == 2.0
        assert median([4.0, 1.0, 3.0, 2.0]) == 2.5

    def test_accepts_ints(self):
        # Delay values from live_completions arrive as ints.
        assert median([180, 300, 420]) == 300


# ═══════════════════════════════════════════════════════════════════════════
# median_and_pstdev
# ═══════════════════════════════════════════════════════════════════════════

class TestMedianAndPstdev:

    def test_empty_returns_zero_pair(self):
        assert median_and_pstdev([]) == (0.0, 0.0)

    def test_single_value_zero_spread(self):
        assert median_and_pstdev([7.0]) == (7.0, 0.0)

    def test_identical_values_zero_spread(self):
        med, sd = median_and_pstdev([4.0, 4.0, 4.0])
        assert med == 4.0
        assert sd == 0.0

    def test_population_not_sample_stddev(self):
        """Variance divides by n (population), not n-1 (sample) —
        matching the hand-rolled blocks this helper replaced."""
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        med, sd = median_and_pstdev(values)
        # Classic textbook population-stddev example: σ = 2 exactly.
        assert sd == pytest.approx(2.0)
        assert med == pytest.approx(4.5)  # even n: (4 + 5) / 2

    def test_matches_naive_formula(self):
        values = [1.0, 2.5, 3.0, 10.0, 4.0]
        med, sd = median_and_pstdev(values)
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        assert sd == pytest.approx(math.sqrt(var))
        assert med == 3.0


# ═══════════════════════════════════════════════════════════════════════════
# tukey_quartiles
# ═══════════════════════════════════════════════════════════════════════════

class TestTukeyQuartiles:

    def test_empty_returns_zeros(self):
        assert tukey_quartiles([]) == (0, 0, 0, 0, 0)

    def test_single_value(self):
        # Halves are empty → Q1/Q3 fall back to min/max.
        assert tukey_quartiles([5.0]) == (5.0, 5.0, 5.0, 5.0, 5.0)

    def test_two_values(self):
        # n=2: median averages; each half is one element.
        assert tukey_quartiles([1.0, 3.0]) == (1.0, 1.0, 2.0, 3.0, 3.0)

    def test_odd_count_excludes_middle_from_halves(self):
        # n=5: halves are [1,2] and [4,5]; each half's "median" is its
        # UPPER middle element (half[len//2]) — Tukey as implemented in
        # the boxplot code this was extracted from.
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert tukey_quartiles(vals) == (1.0, 2.0, 3.0, 5.0, 5.0)

    def test_even_count_includes_middle_in_upper_half(self):
        # n=4: lower=[1,2], upper=[3,4]; median=(2+3)/2.
        vals = [1.0, 2.0, 3.0, 4.0]
        assert tukey_quartiles(vals) == (1.0, 2.0, 2.5, 4.0, 4.0)

    def test_unsorted_input(self):
        vals = [4.0, 1.0, 3.0, 2.0]
        assert tukey_quartiles(vals) == (1.0, 2.0, 2.5, 4.0, 4.0)

    def test_not_statistics_quantiles(self):
        """Pin the median-of-halves semantics: statistics.quantiles
        interpolates and would give different Q1/Q3 here."""
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        lo, q1, med, q3, hi = tukey_quartiles(vals)
        assert (lo, med, hi) == (1.0, 4.0, 7.0)
        # halves [1,2,3] and [5,6,7] → upper-middle picks 2 and 6.
        assert (q1, q3) == (2.0, 6.0)
