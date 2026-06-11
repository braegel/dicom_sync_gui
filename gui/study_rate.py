"""
Pure studies-per-hour rate logic for the dashboard's "Studies / Hour"
section.

Kept Qt-free and side-effect-free (apart from a debug log line) so the
counting and colour-banding rules can be unit-tested without a widget:
``compute_study_rates`` takes the relevant config values as plain
arguments instead of reading an ``AppConfig``.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger("dicom_sync")

# Colour band palette for the study-rate labels.  Defined here (not in
# gui.dashboard) so this module stays import-leaf; the dashboard
# re-imports them as part of its general palette, keeping the hex
# values defined in exactly one place.
COLOR_GREEN = "#2ecc71"
COLOR_YELLOW = "#f1c40f"
COLOR_RED = "#e74c3c"

# Studies-per-hour thresholds that switch the study rate label colour
# and trigger the high-load popup.
#
# Empirically tuned for mid-sized radiology workflows with 1–2 source
# PACS feeds:
#   ≤ 5  studies/h — calm, green
#   ≤ 11 studies/h — busy but tractable, yellow
#   ≥ 12 studies/h — sustained burst → red + popup to warn the user
#                   that the system may not keep up with reading flow
# Adjust here if a different deployment's workload doesn't match.
STUDY_RATE_GOOD_MAX = 5      # green at ≤
STUDY_RATE_WARN_MAX = 11     # yellow at ≤
STUDY_RATE_HIGH_LOAD = 12    # red + popup at ≥


def compute_study_rates(queue: list, *,
                        filter_groups_enabled: bool,
                        institution_assignments: dict,
                        now: datetime | None = None) -> dict[str, int]:
    """Count unique studies within the last 60 minutes, grouped.

    *queue* is a list of study-level dicts (raw query results, NOT
    SeriesJob dicts).  When *filter_groups_enabled* is True the counts
    are keyed by filter-group name (institution → group via
    *institution_assignments*, unassigned institutions under ``""``);
    otherwise everything is pooled under the single key ``"_total"``.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(minutes=60)
    seen: dict[str, set] = {}  # group -> set of study_uids

    for job in queue:
        sd = job.get("study_date") or ""
        # ``or ""`` so a job dict that ever carried an explicit
        # ``study_time=None`` (engine currently never does, but
        # ``.get("study_time", "")`` would still return None then)
        # doesn't crash ``.ljust`` with AttributeError.
        st = (job.get("study_time") or "").ljust(6, "0")[:6]
        if not sd:
            continue
        try:
            dt = datetime.strptime(f"{sd}{st}", "%Y%m%d%H%M%S")
        except ValueError as e:
            # Surface in the log so an unexpected DICOM time
            # format doesn't silently zero the studies/hour
            # display — a user with 10 studies in the queue but
            # "0 studies/h" otherwise has no way to debug this.
            logger.debug(
                f"compute_study_rates: could not parse "
                f"study_date+study_time {sd!r}+{st!r}: {e}")
            continue
        if dt <= cutoff:
            continue

        if filter_groups_enabled:
            group = institution_assignments.get(
                job.get("institution_name", ""), "")
        else:
            group = "_total"

        seen.setdefault(group, set()).add(job.get("study_uid", ""))

    return {g: len(uids) for g, uids in seen.items()}


def study_rate_color(n: int) -> str | None:
    """Return CSS color string for a study rate value."""
    if n <= 0:
        return None
    if n <= STUDY_RATE_GOOD_MAX:
        return COLOR_GREEN
    if n <= STUDY_RATE_WARN_MAX:
        return COLOR_YELLOW
    return COLOR_RED
