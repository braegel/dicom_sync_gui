"""
Pure queue-planning helpers for the DICOM transfer engine.

These functions decide the ORDER in which queued series are downloaded.
They are intentionally free of Qt, threading, and any TransferEngine
state: they take a list of job objects (anything with ``is_prior``,
``study_uid``, ``series_uid``, ``series_description`` and ``remote_count``
attributes) plus a list of priority-term dicts, and return a reordered
list.  Keeping them pure makes the queue-ordering rules independently
testable and keeps the TransferEngine class focused on the service loop
and transfer execution.

``TransferEngine`` re-exports ``AXIAL_PRIORITY_PATTERN`` /
``FIRST_SERIES_MIN_IMAGES`` and exposes thin staticmethod wrappers
(``_sort_jobs_by_priority``, ``_compile_matchers``,
``_compute_study_priorities``, ``_is_axial``,
``_first_substantial_series_uids``) that delegate here, so existing call
sites and tests keep working unchanged.
"""

import logging
import re
from typing import Any, Dict, List, Set

logger = logging.getLogger("dicom_sync")


# Series whose description matches this pattern (case-insensitive,
# "ax" at a word start: "Axial", "ax 3mm", "T2 ax" — but NOT "Thorax")
# are transferred before all other series of the same priority tier —
# across studies and patients.  Axial reconstructions are the primary
# reading series; pulling them first means every study becomes readable
# as early as possible while reformats backfill afterwards.
AXIAL_PRIORITY_PATTERN = re.compile(r"\bax", re.IGNORECASE)

# The first series of each study with more than this many remote images
# is pulled before everything else (even before the axial fast-lane) —
# across studies and patients.  This gets one substantial, viewable
# series of every study on screen as early as possible, so a reader can
# triage all pending studies before any single one finishes in full.
FIRST_SERIES_MIN_IMAGES = 10


def is_axial(series_description: str) -> bool:
    """True when *series_description* contains "ax" at a word
    start (case-insensitive): "Axial", "T2 ax 3mm", "AX T2".
    Words merely containing/ending in "ax" ("Thorax", "max") do
    not count."""
    return bool(AXIAL_PRIORITY_PATTERN.search(
        series_description or ""))


def first_substantial_series_uids(jobs: List[Any]) -> Set[str]:
    """Return the set of ``series_uid``s that are, per study, the
    FIRST series (in original queue order) whose ``remote_count``
    exceeds ``FIRST_SERIES_MIN_IMAGES``.

    Exactly one series per study qualifies (the first that clears
    the threshold); studies whose series are all small contribute
    none.  These lead the queue so every study gets one viewable
    series early — across studies and patients."""
    seen_studies: Set[str] = set()
    result: Set[str] = set()
    for j in jobs:
        if j.study_uid in seen_studies:
            continue
        if j.remote_count > FIRST_SERIES_MIN_IMAGES:
            seen_studies.add(j.study_uid)
            result.add(j.series_uid)
    return result


def compile_matchers(terms: List[Dict[str, Any]],
                     log_label: str = "") -> list:
    """Build one matcher per term.  Each matcher takes a series
    description and returns ``True`` if it matches.  Invalid
    regexes are logged and become permanently-False matchers."""
    prefix = f"[{log_label}] " if log_label else ""
    matchers = []
    for entry in terms:
        t = entry.get("term", "") if isinstance(entry, dict) else ""
        if not t:
            matchers.append(lambda _s: False)
            continue
        if entry.get("is_regex"):
            try:
                pat = re.compile(t, re.IGNORECASE)
                matchers.append(
                    lambda s, p=pat: bool(p.search(s or "")))
            except re.error as exc:
                # Visible in the log so the user can tell why a
                # priority entry no longer biases the queue.
                logger.warning(
                    f"{prefix}priority regex {t!r} did not "
                    f"compile: {exc} — the entry will match "
                    f"nothing this cycle")
                matchers.append(lambda _s: False)
        else:
            needle = t.casefold()
            matchers.append(
                lambda s, n=needle: n in (s or "").casefold())
    return matchers


def compute_study_priorities(
        jobs: List[Any], matchers: list) -> Dict[str, int]:
    """Per study_uid: index of the FIRST matcher that fires on any
    of its series descriptions.  Studies with no match get the
    sentinel ``len(matchers)`` and end up at the bottom."""
    sentinel = len(matchers)
    out: Dict[str, int] = {}
    for j in jobs:
        best = sentinel
        for i, m in enumerate(matchers):
            if m(j.series_description):
                best = i
                break
        # Keep the *lowest* matcher index seen across the study's
        # series.  ``study_uid not in out`` handles the first
        # series we see for this study (no prior value), then the
        # subsequent ones tighten ``best`` if they hit a higher-
        # priority term.
        if j.study_uid not in out or best < out[j.study_uid]:
            out[j.study_uid] = best
    return out


def sort_jobs_by_priority(jobs: List[Any],
                          terms: List[Dict[str, Any]],
                          log_label: str = "") -> List[Any]:
    """Pure stable-sort: studies that contain at least one
    priority-matching series float to the top, in the order the
    terms appear.

    Prior studies (``is_prior``) always sort BELOW every current
    study, even when one of their series matches a priority term —
    a Voruntersuchung must never delay a fresh study.  Within the
    prior block the term order applies again.

    Within each (is_prior, study-priority) tier two fast-lanes
    apply, across studies and patients:
      1. the FIRST series of each study with more than
         ``FIRST_SERIES_MIN_IMAGES`` remote images leads — one
         substantial, viewable series of every study arrives first
         so a reader can triage all pending studies early;
      2. then axial series (description matches
         ``AXIAL_PRIORITY_PATTERN``) — the primary reading series.
    Both rules deliberately break study grouping.

    Composed of small helpers, each independently testable:
    ``compile_matchers`` → ``compute_study_priorities`` /
    ``first_substantial_series_uids`` → stable-sort.
    """
    matchers = compile_matchers(terms, log_label)
    study_prio = compute_study_priorities(jobs, matchers)
    first_substantial = first_substantial_series_uids(jobs)
    # Stable sort by (is_prior, study_priority, not-first-substantial,
    # non-axial, original_position):
    #   - current studies always precede priors,
    #   - priority studies float to the top in priority order,
    #   - each study's first >10-image series leads its tier,
    #   - axial series come next within their tier, across patients,
    #   - within the same slot, original order is kept.
    indexed = list(enumerate(jobs))
    indexed.sort(
        key=lambda pair: (pair[1].is_prior,
                          study_prio[pair[1].study_uid],
                          pair[1].series_uid not in first_substantial,
                          not is_axial(pair[1].series_description),
                          pair[0]))
    return [j for _, j in indexed]
