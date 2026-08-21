"""
Active-transfer state, the stall watchdog, and the pending-restart
bookkeeping for one source's download service.

Extracted from ``gui.dashboard``.  The widget used to carry eight loose
attributes (``_active_series_uid``, ``_active_series_done``,
``_last_progress_ts``, ``_watchdog_deadline_ts``, ``_restart_pending``,
``_restart_params``, ``_restart_is_auto``, ``_pacs_connection_lost``)
that eight different methods mutated in combination — the kind of state
soup where a missed reset only shows up as a spurious service restart
in production.

The pieces here are deliberately Qt-free and side-effect-free: they hold
state and answer questions.  The dashboard keeps the QTimers, the
sounds, the dialogs and the signal emits, because those are the parts
that genuinely belong to a widget.

Why the watchdog is shaped the way it is
----------------------------------------
A "no sub-operation status for 10 s" rule was tried first and was wrong:
a C-MOVE on a large series (a 300-image 3D MR) routinely sends nothing
for well over 10 s while transferring perfectly happily, so the watchdog
fired mid-download and restarted the service.  The rule now needs BOTH
conditions to hold before declaring a wedge:

1. the series blew a deadline derived from its OWN size and the measured
   throughput (generously clamped), AND
2. no per-image progress arrived for ``WATCHDOG_NO_PROGRESS_S``.

A transfer still reporting sub-operations is alive even if it overran
the estimate, so (2) is what keeps a merely slow series running.
"""

import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

# The watchdog polls on this interval (ms); each tick re-checks the
# active series against its deadline.
WATCHDOG_POLL_MS = 15_000
# A series may take up to this multiple of its expected duration
# (images ÷ measured images/sec) before it counts as wedged.
WATCHDOG_DURATION_FACTOR = 5.0
# Absolute floor for the per-series deadline (s): no series is ever
# flagged before this, regardless of size/rate — covers cold stats
# (no rate yet) and small series whose expected duration is tiny.
WATCHDOG_MIN_DEADLINE_S = 180.0
# Hard cap on the per-series deadline (s) so a pathological rate
# estimate can't push it to effectively-never.
WATCHDOG_MAX_DEADLINE_S = 1800.0
# The watchdog also requires this long with NO delivered progress before
# firing — see the module docstring.
WATCHDOG_NO_PROGRESS_S = 120.0


def series_deadline_s(pending_images: int, rate: float) -> float:
    """Plausible maximum wall time (s) for a series of *pending_images*
    at *rate* images/second before it counts as wedged.

    With no rate yet (cold stats) or no images the floor applies, so a
    large series at session start is never flagged early.
    """
    if rate > 0 and pending_images > 0:
        deadline = WATCHDOG_DURATION_FACTOR * (pending_images / rate)
    else:
        deadline = WATCHDOG_MIN_DEADLINE_S
    return max(WATCHDOG_MIN_DEADLINE_S,
               min(deadline, WATCHDOG_MAX_DEADLINE_S))


def pending_images_of(info: dict) -> int:
    """Images still to fetch for the series described by a
    ``SeriesJob.to_dict()`` mapping, defensively: a PACS that sends a
    non-numeric count must not break arming the watchdog."""
    try:
        return max(int(info.get("remote_count", 0))
                   - int(info.get("local_count", 0)), 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class ActiveTransfer:
    """The series currently in flight, and everything derived from it.

    One owner for state that two features share: the stall watchdog
    (deadline, progress clock) and the live Pending / ETE countdown
    (``images_done``).  They used to be four independent attributes on
    the widget, which meant every disarm path had to remember to reset
    all four.
    """

    series_uid: Optional[str] = None
    #: Images received so far in this run, from the latest progress emit.
    images_done: int = 0
    #: Monotonic timestamp of the last real progress; 0 = none yet.
    last_progress_ts: float = 0.0
    #: Monotonic deadline after which the series is *eligible* to be
    #: called wedged; 0 = disarmed.
    deadline_ts: float = 0.0

    @property
    def is_active(self) -> bool:
        return self.series_uid is not None

    @property
    def is_armed(self) -> bool:
        """True when a deadline has been set — a transfer with no
        deadline can never be judged wedged."""
        return bool(self.deadline_ts)

    def begin(self, series_uid: Optional[str], deadline_s: float,
              now: Optional[float] = None) -> None:
        """Arm for a freshly started series."""
        now = time.monotonic() if now is None else now
        self.series_uid = series_uid
        self.images_done = 0
        self.last_progress_ts = now
        self.deadline_ts = now + deadline_s

    def note_progress(self, completed: int,
                      now: Optional[float] = None) -> None:
        """Record a per-image progress tick.

        The deadline set at series start is deliberately left intact —
        genuine progress simply proves the series is not wedged, and
        extending the deadline on every image would make the size-based
        estimate meaningless.
        """
        self.images_done = max(completed, 0)
        self.last_progress_ts = time.monotonic() if now is None else now

    def clear(self) -> None:
        """Disarm completely — no transfer is in flight."""
        self.series_uid = None
        self.images_done = 0
        self.last_progress_ts = 0.0
        self.deadline_ts = 0.0

    def seconds_without_progress(self,
                                 now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        return now - self.last_progress_ts if self.last_progress_ts else 0.0

    def wedged_for(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds without progress if this transfer is GENUINELY
        wedged, otherwise ``None``.

        Both conditions from the module docstring must hold.  Returning
        the elapsed time rather than a bare bool lets the caller put a
        real number in its log line and popup.
        """
        if not self.is_active or not self.is_armed:
            return None
        now = time.monotonic() if now is None else now
        if now < self.deadline_ts:
            return None
        without_progress = self.seconds_without_progress(now)
        if without_progress < WATCHDOG_NO_PROGRESS_S:
            return None
        return without_progress

    def pending_for(self, job: dict,
                    terminal_statuses: Sequence[str]) -> int:
        """Pending images for one queue row, discounting what this
        transfer has already received so the count drops live."""
        if job["status"] in terminal_statuses:
            return 0
        pending = max(job["remote_count"] - job["local_count"], 0)
        if job["series_uid"] == self.series_uid and self.images_done > 0:
            pending = max(pending - self.images_done, 0)
        return pending


@dataclass
class PendingRestart:
    """A Restart that is waiting for the engine to actually stop.

    ``params`` snapshots the spinbox values at click time so the engine
    comes back up with the parameters the user actually saw when they
    pressed Restart — not whatever they fiddled the controls to during
    the C-MOVE wind-down window.

    ``is_auto`` marks a watchdog-triggered restart (vs. the manual
    button).  It is forwarded to the auto-start path so MainWindow keeps
    retrying-with-siren on an unreachable PACS instead of aborting.
    """

    params: Any
    is_auto: bool = False


# What a dashboard should do when a pending Restart can finally be
# acted on.  Two call sites reach that point — the engine reporting it
# has stopped, and the safety timer firing because it never did — and
# they used to re-implement the same three-way branch independently.
RESUME = "resume"        # bring the service back up with the snapshot
CANCEL_SOURCE_GONE = "cancelled"   # source was deleted meanwhile
NOTHING_PENDING = "none"           # no Restart was waiting


def resolve_pending_restart(pending: Optional["PendingRestart"],
                            source_exists: bool) -> str:
    """Decide what to do with *pending* now that the engine is idle.

    Returns one of ``RESUME`` / ``CANCEL_SOURCE_GONE`` /
    ``NOTHING_PENDING``.

    The source-removed case is its own outcome rather than a silent
    ``RESUME``: MainWindow drops a ``start_requested`` for a source
    that no longer exists, which would leave the user staring at a
    half-finished restart with no explanation.
    """
    if pending is None:
        return NOTHING_PENDING
    if not source_exists:
        return CANCEL_SOURCE_GONE
    return RESUME
