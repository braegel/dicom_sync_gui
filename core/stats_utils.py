"""
Shared statistics helpers for DICOM Sync GUI.

Consolidates the median / population-stddev / quartile math that used
to be hand-rolled in transfer_engine, transfer_log, live_completions
and transfer_stats_window.  All callers in this project agree on the
same conventions, which these helpers pin down:

* **median** — average-of-the-middle-pair for even-length input
  (exactly what ``statistics.median`` does), ``0.0`` for empty input
  (the project-wide "no data" sentinel; ``statistics.median`` would
  raise instead).
* **stddev** — POPULATION standard deviation (variance divided by
  ``n``, not ``n - 1``), matching ``statistics.pstdev``.
* **quartiles** — Tukey median-of-halves (each half's middle element,
  upper-middle for even halves), NOT ``statistics.quantiles`` which
  interpolates differently.  Preserved verbatim from the boxplot code
  in transfer_stats_window.

Pure functions, no Qt — safe to import from core and gui alike.
"""

import statistics
from typing import Sequence, Tuple

__all__ = ["median", "median_and_pstdev", "tukey_quartiles"]


def median(values: Sequence[float]) -> float:
    """Median of *values*; ``0.0`` for empty input.

    Even-length input averages the two middle elements (the
    ``statistics.median`` convention shared by every call site in
    this project).  Input does not need to be sorted.
    """
    if not values:
        return 0.0
    return statistics.median(values)


def median_and_pstdev(values: Sequence[float]) -> Tuple[float, float]:
    """Return ``(median, population_stddev)`` of *values*.

    Population stddev divides the variance by ``n`` (matching the
    hand-rolled ``sum((v - mean)**2) / n`` blocks this replaces).
    Empty input returns ``(0.0, 0.0)``; a single value returns
    ``(value, 0.0)`` — pstdev of one sample is zero spread.
    """
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        # statistics.pstdev requires >= 1 point and is happy with one,
        # but spell the case out: spread of a single sample is 0.
        return float(values[0]), 0.0
    return statistics.median(values), statistics.pstdev(values)


def tukey_quartiles(
        values: Sequence[float]
) -> Tuple[float, float, float, float, float]:
    """Return ``(min, Q1, median, Q3, max)`` using Tukey's
    median-of-halves method; ``(0, 0, 0, 0, 0)`` for empty input.

    The halves exclude the middle element for odd *n*; each half's
    "median" is its upper-middle element (``half[len(half) // 2]``),
    exactly preserving the boxplot semantics this was extracted from.
    Do NOT replace with ``statistics.quantiles`` — that interpolates
    between elements and would shift every box on the chart.
    """
    n = len(values)
    if n == 0:
        return (0, 0, 0, 0, 0)
    s = sorted(values)
    mid = n // 2
    med = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
    lower = s[:mid]
    upper = s[mid + 1:] if n % 2 else s[mid:]
    q1 = lower[len(lower) // 2] if lower else s[0]
    q3 = upper[len(upper) // 2] if upper else s[-1]
    return (s[0], q1, med, q3, s[-1])
