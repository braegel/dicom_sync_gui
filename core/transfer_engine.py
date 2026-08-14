"""
Transfer engine for DICOM Sync GUI.
Each TransferEngine instance serves exactly one source PACS node.
Runs a continuous service loop: query → compare → transfer → wait → repeat.
Emits Qt signals so the GUI can display queue and progress in real time.
"""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QObject, Signal
from pydicom import Dataset

from core import queue_planner
from core.dicom_ops import DicomOperations, PacsConnectionError
from core.stats_utils import median
from core.transfer_log import TransferLog, default_db_path

logger = logging.getLogger("dicom_sync")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SeriesJob:
    """One series that needs to be transferred."""
    patient_name: str = ""
    patient_id: str = ""
    study_description: str = ""
    series_description: str = ""
    modality: str = ""
    series_number: str = ""
    study_uid: str = ""
    series_uid: str = ""
    remote_count: int = 0
    local_count: int = 0
    # queued, transferring, done, error, skipped, unavailable
    # ("unavailable" = retry budget exhausted, will not be fetched again)
    status: str = "queued"
    institution_name: str = ""
    accession_number: str = ""
    images_per_minute: float = 0.0
    transferred_images: int = 0
    duration_seconds: float = 0.0
    study_date: str = ""
    study_time: str = ""
    # When the series itself was created on the modality (DICOM
    # SeriesDate / SeriesTime, ``YYYYMMDD`` / ``HHMMSS``).  Falls back to
    # the study date/time when the PACS doesn't return series-level
    # values.  Shown in the queue's "Series Created" column.
    series_date: str = ""
    series_time: str = ""
    # True for series of a prior study (Voruntersuchung).  Priors are
    # always transferred AFTER all current studies — even when one of
    # their series matches a configured priority term.
    is_prior: bool = False

    @property
    def to_transfer(self) -> int:
        return max(self.remote_count - self.local_count, 0)

    def to_dict(self) -> dict:
        return {
            "patient_name": self.patient_name,
            "patient_id": self.patient_id,
            "study_description": self.study_description,
            "series_description": self.series_description,
            "modality": self.modality,
            "series_number": self.series_number,
            "study_uid": self.study_uid,
            "series_uid": self.series_uid,
            "remote_count": self.remote_count,
            "local_count": self.local_count,
            "status": self.status,
            "institution_name": self.institution_name,
            "accession_number": self.accession_number,
            "images_per_minute": self.images_per_minute,
            # Per-series totals so a GUI consumer can sum them itself
            # instead of reaching back into the engine's live jobs.
            "transferred_images": self.transferred_images,
            "duration_seconds": self.duration_seconds,
            "study_date": self.study_date,
            "study_time": self.study_time,
            "series_date": self.series_date,
            "series_time": self.series_time,
            "is_prior": self.is_prior,
        }


@dataclass
class SeriesCompletionRecord:
    """Stores the measured speed for one completed series."""
    series_uid: str = ""
    image_count: int = 0
    duration_seconds: float = 0.0
    images_per_minute: float = 0.0


# Terminal SeriesJob.status values: a series in one of these states can
# no longer change, contributes no pending images, and gets no ETE.
# Single source of truth shared by the engine and the GUI (dashboard /
# main_window) so the set cannot drift between them.
TERMINAL_STATUSES = ("done", "error", "skipped", "unavailable")

# Series with fewer than this many remote images may fail (error/
# skipped) without blocking the study's fully_complete signal — small
# series are typically localizers or stuck tiny series that should not
# silently suppress the Download Completions entry for everything else
# that did arrive.
SMALL_SERIES_MAX_IMAGES_FOR_COMPLETION = 6

# A series is retried at most this many times before it is marked
# permanently unavailable (status "unavailable").  Small series that
# fail to transfer for DICOM-level reasons would otherwise be retried
# every cycle forever.
MAX_SERIES_TRANSFER_ATTEMPTS = 3

# Minimum number of NEW images a re-completion must add before
# study_completed re-fires and advances the Download Completions
# "Download Completed" timestamp.  Single-image retries and re-sends
# (1–5 images) are stragglers that should not move the row's completion
# time — only a real wave of new images does.
#
# "New images" is measured against ``_study_images_transferred``, which
# counts images the source PACS actually REPORTED as completed
# sub-operations, summed across ALL cycles.  Both properties matter:
# a per-cycle counter would let every retry cycle refresh the timestamp
# for free, and counting optimistic image numbers (see _record_success)
# would let a C-MOVE that moved nothing look like a full transfer.
MIN_IMAGES_TO_REFRESH_COMPLETION = 5

# Upper bound on how many studies the engine keeps completion history
# for (``_completed_studies`` / ``_study_images_transferred``).  Those
# dicts must survive across cycles — that is exactly what makes the rule
# above work for retried stragglers — so they cannot simply be cleared,
# and over a long-running service they would otherwise grow one entry
# per study forever.
#
# Time-window pruning would look natural ("drop what the query can no
# longer return") but is NOT safe: an old study can re-enter the queue
# at any time as a PRIOR of a fresh study, and a pruned study's next
# completion would look like a FIRST completion and wrongly refresh its
# Download Completions timestamp — the very bug the dicts prevent.  So
# eviction is by least-recent activity instead, and
# ``_prune_study_history`` never evicts a study that is still in the
# queue.  The tradeoff: a study could in principle lose its history if
# more than this many OTHER studies complete between two of its retry
# cycles.  At realistic volumes (a busy site sees a few hundred studies
# a day) that cannot happen within a day, which is the horizon the
# straggler rule cares about.
MAX_TRACKED_STUDIES = 5000

# How many CONSECUTIVE cycles a study's series query may come back
# truncated before the engine stops deferring its completion verdict.
#
# Deferring is right for a one-off blip: "all series terminal" would
# otherwise be a verdict about series the PACS never got to enumerate.
# But a source that truncates every cycle would defer forever, and the
# study would keep downloading images while never appearing in Download
# Completions — a silent disappearance that is worse than a slightly
# early verdict.  After this many attempts the partial view is accepted
# and the shortfall is logged.
MAX_INCOMPLETE_QUERY_CYCLES = 3

# Minimum wall-clock gap between two ``series_progress`` emits during a
# single C-MOVE.  pynetdicom yields one sub-operation status PER IMAGE;
# emitting a queued cross-thread signal for every image of a large
# series floods the GUI thread's event queue and pins it at 100% CPU
# (the UI freezes until the series finishes).  Throttling to a few
# updates per second keeps the stall watchdog and live Pending/ETE
# counters responsive without the storm.  A final emit is always sent
# when the series completes, regardless of this interval.
PROGRESS_EMIT_INTERVAL_S = 0.5

# Queue-ordering rules (axial fast-lane, first-substantial fast-lane,
# priority terms) live in the pure, Qt-free core.queue_planner module.
# Re-exported here so existing importers of these names from
# core.transfer_engine keep working.
AXIAL_PRIORITY_PATTERN = queue_planner.AXIAL_PRIORITY_PATTERN
FIRST_SERIES_MIN_IMAGES = queue_planner.FIRST_SERIES_MIN_IMAGES


@dataclass
class TransferStats:
    """Per-series throughput statistics with median aggregation.

    Only series with at least ``MIN_IMAGES_FOR_STATS`` images are
    included in the speed statistics.  Smaller series are still counted
    towards ``total_images`` but their transfer speed is too noisy to
    be meaningful.

    Thread safety: instances are mutated on the engine's service-loop
    thread (``start_session``, ``record_series``) while the GUI thread
    reads them concurrently — the ``stats_updated`` signal emits the
    LIVE object and the dashboard polls it from a QTimer.  All public
    methods therefore take ``_lock`` so ``_completed_series`` cannot
    grow mid-iteration under a reader.  Locked public methods must NOT
    call each other (plain ``Lock``, not ``RLock``); shared logic lives
    in ``_*_unlocked`` helpers that assume the lock is already held.
    """
    MIN_IMAGES_FOR_STATS: ClassVar[int] = 10

    total_images: int = 0
    start_time: float = 0.0
    _completed_series: List["SeriesCompletionRecord"] = field(
        default_factory=list)
    # Guards every access to total_images / start_time /
    # _completed_series.  Excluded from repr/compare so the dataclass
    # niceties keep working (locks are neither printable state nor
    # comparable).
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False)

    def start_session(self):
        with self._lock:
            self.start_time = time.time()
            self.total_images = 0
            self._completed_series = []

    def record_series(self, series_uid: str, image_count: int,
                      duration_seconds: float):
        """Record a completed series transfer with its measured speed.

        The series is always appended (so ``completed_count`` reflects
        every finished transfer), but series below
        ``MIN_IMAGES_FOR_STATS`` images are flagged so the statistics
        methods can skip them.
        """
        ipm = (image_count / duration_seconds) * 60 if duration_seconds > 0 else 0.0
        record = SeriesCompletionRecord(
            series_uid=series_uid,
            image_count=image_count,
            duration_seconds=duration_seconds,
            images_per_minute=ipm,
        )
        with self._lock:
            self.total_images += image_count
            self._completed_series.append(record)

    @property
    def completed_count(self) -> int:
        with self._lock:
            return len(self._completed_series)

    def _stats_series_unlocked(self) -> List["SeriesCompletionRecord"]:
        """Completed series that qualify for speed statistics.

        Caller must hold ``_lock`` — this iterates
        ``_completed_series``, which the service-loop thread appends to.
        Returns a fresh list, so the snapshot stays consistent after the
        lock is released."""
        return [r for r in self._completed_series
                if r.image_count >= self.MIN_IMAGES_FOR_STATS]

    @property
    def _stats_series(self) -> List["SeriesCompletionRecord"]:
        """Locked public view of the qualifying series (kept for tests
        and introspection; internal code uses the unlocked helper)."""
        with self._lock:
            return self._stats_series_unlocked()

    def raw_images_per_minute(self) -> float:
        """Overall images/minute since session start.

        Reads ``start_time`` and ``total_images`` in a single locked
        snapshot so callers on another thread get a consistent rate."""
        with self._lock:
            total = self.total_images
            start = self.start_time
        if not start or total <= 0:
            return 0.0
        elapsed = time.time() - start
        if elapsed <= 0:
            return 0.0
        return total / elapsed * 60.0

    def last_series_ipm(self) -> float:
        """Images/minute for the most recently completed qualifying series."""
        with self._lock:
            qualifying = self._stats_series_unlocked()
        if not qualifying:
            return 0.0
        return qualifying[-1].images_per_minute

    @staticmethod
    def _median(values: List[float]) -> float:
        """Thin delegate to ``core.stats_utils.median`` — kept as a
        staticmethod because tests (and possibly external callers)
        reference ``TransferStats._median`` directly."""
        return median(values)

    def median_n_ipm(self, n: int) -> float:
        """Median images/minute over the last *n* qualifying series."""
        with self._lock:
            qualifying = self._stats_series_unlocked()
        if not qualifying:
            return 0.0
        recent = qualifying[-n:]
        return median([r.images_per_minute for r in recent])

    def median_all_ipm(self) -> float:
        """Median images/minute across all qualifying series."""
        with self._lock:
            qualifying = self._stats_series_unlocked()
        return median(
            [r.images_per_minute for r in qualifying])

    def overall_images_per_minute(self) -> float:
        """Overall images/minute (used for ETE calculation).

        Returns the median over all qualifying series.  Falls back to 0
        when no qualifying series have finished yet.
        """
        return self.median_all_ipm()


# ---------------------------------------------------------------------------
# Qt signals
# ---------------------------------------------------------------------------

class TransferSignals(QObject):
    """Signals emitted by the engine for the GUI."""
    # Queue was rebuilt after a query cycle
    queue_updated = Signal(list)          # list[SeriesJob.to_dict()]
    # A single series transfer started
    series_started = Signal(dict)         # SeriesJob.to_dict()
    # Progress within a series
    series_progress = Signal(str, int, int)  # series_uid, transferred, total
    # Series finished successfully
    series_completed = Signal(str, int)      # series_uid, images
    # Series failed
    series_error = Signal(str, str)          # series_uid, error_msg
    # Stats updated
    stats_updated = Signal(object)           # TransferStats
    # Cycle status
    cycle_started = Signal(int)              # cycle number
    cycle_finished = Signal(int, int)        # cycle number, images this cycle
    # Service lifecycle
    service_started = Signal()
    service_stopped = Signal()
    # Manual selection mode: engine paused, waiting for user to pick series
    queue_ready_for_selection = Signal(list)  # list[SeriesJob.to_dict()]
    # Log
    log_message = Signal(str)
    # Unknown institution detected (institution_name)
    unknown_institution = Signal(str)
    # Raw query results — study-level dicts for study rate display
    studies_queried = Signal(list)  # list[{study_uid, study_date, study_time, institution_name}]
    # All series for a patient (incl. priors) finished downloading
    patient_studies_completed = Signal(str, str)  # patient_id, institution_name
    # All series of a single study finished downloading
    study_completed = Signal(str, str, bool, int)  # study_uid, institution_name, fully_complete, total_images_done
    # Source (or local) PACS became unreachable during a query/download.
    # Emitted once per outage; the GUI shows an unreachable popup.
    connection_lost = Signal(str, str)  # remote_key, detail message
    # Connection recovered after a previous connection_lost.
    connection_restored = Signal(str)   # remote_key


# ---------------------------------------------------------------------------
# Engine — one instance per source PACS
# ---------------------------------------------------------------------------

class TransferEngine:
    """
    Single-source service: query one remote → build queue → transfer → sleep → repeat.
    Create one instance per configured source PACS node.
    """

    def __init__(self, config: Any, remote_key: str,
                 transfer_log: Optional[TransferLog] = None):
        self.config = config
        self.remote_key = remote_key
        self.signals = TransferSignals()
        self.stats = TransferStats()
        self._transfer_log = (transfer_log if transfer_log is not None
                              else TransferLog(default_db_path()))
        self._dicom_ops: Optional[DicomOperations] = None
        self._init_service_state()
        self._init_study_history()

    def _init_service_state(self) -> None:
        """State of the running service: the thread, the cancel flag,
        the queue, and the manual-selection handshake.  Reset shape for
        the whole engine lifetime — a stopped engine keeps it, a
        restarted service gets a whole new engine."""
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._queue: List[SeriesJob] = []
        # Guards whole-list reassignment vs. snapshot reads from the GUI
        # thread.  Per-job attribute mutations (e.g. ``job.status``) remain
        # unlocked; snapshot consumers must treat returned jobs as
        # point-in-time views that may already be stale.
        self._queue_lock = threading.Lock()
        self._notified_institutions: Set[str] = set()
        self._selection_mode = False
        self._selection_event = threading.Event()
        self._selected_uids: Set[str] = set()
        # True between a connection_lost emit and the next successful
        # association, so the GUI gets exactly one popup per outage
        # instead of one per failed series/cycle.
        self._connection_lost_notified = False

    def _init_study_history(self) -> None:
        """Per-study bookkeeping that must SURVIVE cycles.

        This is the group that needed a name: every dict here is
        deliberately not cleared per cycle (that is what makes the
        completion-timestamp rule work across retries), which is also
        why they all need pruning — see ``_prune_study_history``.
        """
        self._completed_patients: Set[str] = set()
        # study_uid → value of _study_images_transferred at the moment
        # study_completed last fired.  A later completion re-emits only when
        # more than MIN_IMAGES_TO_REFRESH_COMPLETION further images have
        # actually arrived since then, which advances the Download
        # Completions row's timestamp (and the Copy button behind it) to the
        # latest real image arrival.  Deliberately NOT cleared between
        # cycles: retry cycles are exactly the case this must survive.
        self._completed_studies: Dict[str, int] = {}
        # study_uid → images the source PACS reported as actually
        # transferred for this study, summed over all cycles of this
        # engine's lifetime.  This is the ONLY input to the re-emit
        # decision above; see MIN_IMAGES_TO_REFRESH_COMPLETION.  Failed
        # attempts and "successful" C-MOVEs that moved zero images add
        # nothing, so they can never move the completion time.
        self._study_images_transferred: Dict[str, int] = {}
        # study_uids whose series C-FIND came back truncated in the
        # CURRENT cycle — their completion verdict is deferred to the
        # next cycle.  Rebuilt by every _query_source call.
        self._incomplete_series_queries: Set[str] = set()
        # study_uid → consecutive cycles whose series query truncated.
        # Reset as soon as one comes back whole; see
        # MAX_INCOMPLETE_QUERY_CYCLES for why the deferral is bounded.
        self._incomplete_query_streak: Dict[str, int] = {}
        # study_uid → accumulated transfer seconds of the study's OWN
        # series (incl. failed attempts).  Used as the study's wall
        # clock: since the axial fast-lane interleaves series of
        # different studies in the queue, first-start-to-last-end
        # would include other studies' downloads.
        self._study_active_seconds: Dict[str, float] = {}
        # study_uid → wall-clock seconds, populated at completion and
        # popped by the GUI.
        self._study_wall_clock: Dict[str, float] = {}
        self._study_wall_clock_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def _remote_node(self):
        return self.config.remote_nodes[self.remote_key]

    @property
    def _active_node_or_none(self):
        """Return the engine's ``PacsNode`` or ``None`` if the source
        has been removed from the config (vs. ``_remote_node`` which
        raises ``KeyError`` in that case)."""
        return self.config.remote_nodes.get(self.remote_key)

    def queue_snapshot(self) -> List[SeriesJob]:
        """Return a shallow copy of the current job queue.

        Safe to call from any thread: the snapshot is taken under
        ``_queue_lock`` so the returned list is not the same object the
        service-loop thread reassigns.  SeriesJob instances inside are
        shared, so caller must treat their fields as point-in-time."""
        with self._queue_lock:
            return list(self._queue)

    def join(self, timeout: Optional[float] = None) -> bool:
        """Join the service-loop thread.  Returns True if it finished."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def pop_study_wall_clock(self, study_uid: str) -> Optional[float]:
        """Thread-safe pop of the wall-clock duration for a completed study.

        The service-loop thread writes this dict when a study completes;
        the GUI thread reads it in the completion handler.  Holding the
        lock for the pop makes the read/remove atomic."""
        with self._study_wall_clock_lock:
            return self._study_wall_clock.pop(study_uid, None)

    # -- public API ----------------------------------------------------------

    def start(self, hours: int, max_images: int, sync_interval: int,
              selection_mode: bool = False):
        """Start the continuous service loop."""
        if self._running:
            return
        self._cancel.clear()
        self._selection_event.clear()
        self._selection_mode = selection_mode
        self._running = True
        self._thread = threading.Thread(
            target=self._service_loop,
            args=(hours, max_images, sync_interval),
            daemon=True,
        )
        self._thread.start()
        self.signals.service_started.emit()

    def stop(self):
        """Request a graceful stop."""
        self._cancel.set()
        # Unblock any pending selection wait
        self._selection_event.set()

    def confirm_selection(self, selected_uids: list):
        """Called from the GUI: user confirmed which series to download."""
        self._selected_uids = set(selected_uids)
        self._selection_event.set()

    # -- internal ------------------------------------------------------------

    def _log(self, msg: str):
        logger.info(msg)
        # The emit runs on the service-loop thread; if the
        # TransferSignals QObject has already been torn down (test
        # teardown, app shutdown racing a final cycle), PySide raises
        # "Signal source has been deleted".  The file log above has
        # the message either way — same guard as async_helpers.
        try:
            self.signals.log_message.emit(msg)
        except RuntimeError:
            pass

    def _service_loop(self, hours: int, max_images: int, sync_interval: int):
        self.stats.start_session()
        node = self._remote_node
        self._log(f"[{self.remote_key}] Service started — downloading last "
                  f"{hours}h, max {max_images or 'unlimited'} img/series, "
                  f"interval {sync_interval}s")
        cycle = 0
        try:
            while not self._cancel.is_set():
                cycle += 1
                self.signals.cycle_started.emit(cycle)
                images_this_cycle = self._run_one_cycle(
                    hours, max_images)
                self.signals.cycle_finished.emit(cycle, images_this_cycle)

                if images_this_cycle > 0:
                    self._log(f"[{self.remote_key}] Cycle {cycle} done — "
                              f"{images_this_cycle} images transferred.")
                else:
                    self._log(f"[{self.remote_key}] Cycle {cycle} — no new "
                              f"images. Waiting {sync_interval}s...")

                # Sleep in small steps so we can react to cancel quickly
                for _ in range(sync_interval):
                    if self._cancel.is_set():
                        break
                    time.sleep(1)

        except Exception as e:
            self._log(f"[{self.remote_key}] Service error: {e}")
            logger.exception("Service loop error")
        finally:
            # Tear down pynetdicom's AE so its reactor threads exit
            # before we drop the reference.  Letting the AE be GC'd
            # while its reactor threads are still alive is what the
            # ``gc.freeze()`` workaround in main.py exists to mask.
            ops = self._dicom_ops
            if ops is not None:
                ops.close()
            self._dicom_ops = None
            self._running = False
            self._log(f"[{self.remote_key}] Service stopped.")
            self.signals.service_stopped.emit()

    def _run_one_cycle(self, hours: int, max_images: int) -> int:
        """Query the source PACS, build queue, transfer everything."""
        self._completed_patients.clear()
        # _completed_studies / _study_images_transferred are NOT cleared
        # here.  They are what makes the "> MIN_IMAGES_TO_REFRESH_COMPLETION
        # new images" rule work across cycles: a study whose stragglers are
        # retried cycle after cycle must not get a fresh completion
        # timestamp just because a new cycle started.

        # Reuse a single DicomOperations (= one pynetdicom AE) for the
        # lifetime of the engine.  Each AE spawns reactor threads that
        # outlive the Python object; letting the AE be GC'd while those
        # threads are still running causes a SIGSEGV in mark_stacks.
        if self._dicom_ops is None:
            self._dicom_ops = self._make_dicom_ops()
        dicom_ops = self._dicom_ops

        jobs = self._query_and_build_queue(hours, max_images, dicom_ops)
        # Prune AFTER the queue is known (so the studies that can still
        # complete this cycle are protected from eviction) but BEFORE
        # the empty-queue early return, so an idle engine still trims.
        self._prune_study_history([j.study_uid for j in jobs])
        if not jobs:
            return 0

        if self._selection_mode:
            jobs = self._await_user_selection(jobs)
            if not jobs:
                return 0

        with self._queue_lock:
            self._queue = jobs
        self.signals.queue_updated.emit([j.to_dict() for j in jobs])
        self._log(f"[{self.remote_key}] Queue: {len(jobs)} series to download")

        return self._transfer_queue(jobs, dicom_ops)

    def _prune_study_history(self,
                             active_study_uids: List[str]) -> None:
        """Bound the three per-study dicts to ``MAX_TRACKED_STUDIES``
        entries by dropping the studies that have been inactive longest.

        The dicts are ``_completed_studies`` (last-emitted mark),
        ``_study_images_transferred`` (lifetime arrival count) and
        ``_study_active_seconds`` (accumulated wall clock).  None may be
        cleared per cycle (see _run_one_cycle), so this is what keeps a
        long-running service from growing them forever.  The first two
        only ever grow; the third is popped on completion, which means it
        leaks exactly for the studies that never complete — a series
        stuck in retry, or one whose series query keeps truncating.

        Two invariants make the eviction safe:

        * a study still in the queue is never evicted — losing its mark
          mid-flight would make its next completion look like a first
          completion and wrongly refresh the Download Completions
          timestamp;
        * a study is dropped from ALL THREE at once — keeping only the
          "last emitted" mark would permanently suppress its refreshes,
          keeping only the arrival count would fake a first completion.

        An evicted study that later re-enters the queue and completes
        falls back to summing its series' durations for the wall clock
        (see ``_persist_study_record``), which is the same fallback used
        for a study that never accumulated any active time.
        """
        history = (self._completed_studies, self._study_images_transferred,
                   self._study_active_seconds, self._incomplete_query_streak)
        # Re-inserting a key moves it to the END of a dict's insertion
        # order, so "oldest key first" below means "least recently in a
        # queue".  Every study of the current queue counts as active,
        # whether or not it ends up transferring anything this cycle.
        ordered_active = list(dict.fromkeys(active_study_uids))
        active = set(ordered_active)
        for uid in ordered_active:
            for d in history:
                if uid in d:
                    d[uid] = d.pop(uid)
        for d in history:
            if len(d) <= MAX_TRACKED_STUDIES:
                continue
            for uid in list(d):
                if len(d) <= MAX_TRACKED_STUDIES:
                    break
                if uid in active:
                    continue
                for target in history:
                    target.pop(uid, None)

    def _note_series_query_result(self, study_uid: str,
                                  complete: bool) -> None:
        """Record whether this cycle's series C-FIND for *study_uid*
        returned the whole list, and keep the consecutive-failure streak
        in step.

        A complete query clears the streak: only CONSECUTIVE truncations
        count towards MAX_INCOMPLETE_QUERY_CYCLES, so an occasional blip
        never pushes a study over the limit.
        """
        if complete:
            self._incomplete_query_streak.pop(study_uid, None)
            return
        self._incomplete_series_queries.add(study_uid)
        streak = self._incomplete_query_streak.get(study_uid, 0) + 1
        self._incomplete_query_streak[study_uid] = streak
        if streak < MAX_INCOMPLETE_QUERY_CYCLES:
            self._log(f"  [{self.remote_key}] Series query for study "
                      f"{study_uid} was incomplete ({streak}/"
                      f"{MAX_INCOMPLETE_QUERY_CYCLES}) — completion "
                      f"check deferred to the next cycle")
        else:
            self._log(f"  [{self.remote_key}] Series query for study "
                      f"{study_uid} has been incomplete {streak} cycles "
                      f"in a row — accepting the partial series list so "
                      f"the study is not withheld from Download "
                      f"Completions indefinitely")

    def _completion_deferred(self, study_uid: str) -> bool:
        """Whether the completion verdict for *study_uid* must wait for
        a better series list this cycle."""
        if study_uid not in self._incomplete_series_queries:
            return False
        return (self._incomplete_query_streak.get(study_uid, 0)
                < MAX_INCOMPLETE_QUERY_CYCLES)

    def _query_and_build_queue(self, hours: int, max_images: int,
                               dicom_ops: DicomOperations) -> List[SeriesJob]:
        """Run the C-FIND pass and produce the list of series to transfer."""
        now = datetime.now()
        cutoff = now - timedelta(hours=hours)
        # The C-FIND date range must reach back at least to the cutoff,
        # otherwise hours > ~24-48 silently finds nothing: the time
        # filter below would keep studies the query never returned.
        # Keep the historical yesterday-start as the minimum span so
        # short windows still tolerate around-midnight studies.
        range_start = min(now - timedelta(days=1), cutoff)
        date_range = f"{range_start.strftime('%Y%m%d')}-{now.strftime('%Y%m%d')}"
        seen_series: Set[str] = set()

        try:
            jobs = self._query_source(
                date_range, cutoff, max_images, seen_series, dicom_ops)
        except PacsConnectionError as e:
            # The source (or local) PACS went away during the query.
            # Surface it once and treat the cycle as empty; the service
            # loop sleeps and retries, recovering automatically when the
            # PACS comes back.
            self._handle_connection_lost(str(e))
            jobs = []
        except Exception as e:
            self._log(f"  [{self.remote_key}] Error querying: {e}")
            jobs = []
        else:
            # A query that completed (even with zero studies) proves the
            # connection is alive again.
            self._handle_connection_restored()

        if not jobs:
            with self._queue_lock:
                self._queue = []
            self.signals.queue_updated.emit([])
        return jobs

    def _handle_connection_lost(self, detail: str) -> None:
        """Emit ``connection_lost`` once per outage and log it.

        Called from the query and download paths when an association
        cannot be established mid-operation.  The latched flag is
        cleared by :py:meth:`_handle_connection_restored` on the next
        successful association, so each distinct outage produces exactly
        one popup."""
        self._log(f"[{self.remote_key}] PACS connection lost: {detail}")
        if self._connection_lost_notified:
            return
        self._connection_lost_notified = True
        try:
            self.signals.connection_lost.emit(self.remote_key, detail)
        except RuntimeError:
            # Signals QObject torn down during shutdown — see _log.
            pass

    def _handle_connection_restored(self) -> None:
        """Clear the connection-lost latch after a successful
        association and tell the GUI, so a later outage notifies
        again."""
        if not self._connection_lost_notified:
            return
        self._connection_lost_notified = False
        self._log(f"[{self.remote_key}] PACS connection restored.")
        try:
            self.signals.connection_restored.emit(self.remote_key)
        except RuntimeError:
            pass

    def _await_user_selection(
            self, jobs: List[SeriesJob]) -> List[SeriesJob]:
        """Pause for manual selection; return the jobs the user kept."""
        self._selection_event.clear()
        self.signals.queue_ready_for_selection.emit(
            [j.to_dict() for j in jobs])
        while not self._cancel.is_set():
            if self._selection_event.wait(timeout=1.0):
                break
        if self._cancel.is_set():
            return []
        jobs = [j for j in jobs if j.series_uid in self._selected_uids]
        if not jobs:
            with self._queue_lock:
                self._queue = []
            self.signals.queue_updated.emit([])
        return jobs

    def _transfer_queue(self, jobs: List[SeriesJob],
                        dicom_ops: DicomOperations) -> int:
        """Transfer every job in the queue; return total images moved.

        If the PACS becomes unreachable mid-queue, abort the rest of
        the cycle instead of letting every remaining series burn the
        full connect timeout — the service loop will retry next cycle
        and recover automatically once the PACS is back."""
        total_images = 0
        for job in jobs:
            if self._cancel.is_set():
                break
            # Series that exhausted their retry budget are kept in the
            # queue for visibility but never transferred again.
            if job.status == "unavailable":
                continue
            try:
                total_images += self._transfer_series(job, dicom_ops)
            except PacsConnectionError as e:
                job.status = "error"
                self.signals.series_error.emit(
                    job.series_uid, "PACS not reachable")
                self.signals.queue_updated.emit(
                    [j.to_dict() for j in jobs])
                self._handle_connection_lost(str(e))
                break
            self.signals.queue_updated.emit([j.to_dict() for j in jobs])
        return total_images

    def _ensure_dicom_ops(self,
                          dicom_ops: Optional[DicomOperations]
                          ) -> DicomOperations:
        """Return the supplied DicomOperations, or the engine's cached
        instance (lazily constructed).  Centralizes the dual-path so
        callers don't repeat the ``if None: …`` block."""
        if dicom_ops is not None:
            return dicom_ops
        if self._dicom_ops is None:
            self._dicom_ops = self._make_dicom_ops()
        return self._dicom_ops

    def _query_source(self, date_range: str, cutoff: datetime,
                      max_images: int,
                      seen_series: Set[str],
                      dicom_ops: Optional[DicomOperations] = None,
                      ) -> List[SeriesJob]:
        """Query the source PACS and return new SeriesJob items.

        Reads as the sequence of steps it performs: C-FIND → time
        filter → metadata seed → per-study jobs → priors → priority
        ordering → ``studies_queried`` emit.  The last two are ordered
        deliberately; see the comments at those call sites."""
        dicom_ops = self._ensure_dicom_ops(dicom_ops)
        # Populated by the per-study pass below and consumed by the
        # transfer pass (_check_study_complete).  Reset here because the
        # flag is only valid for the queue this query built: a study
        # whose series query failed last cycle must get a fresh verdict
        # from this cycle's query.
        self._incomplete_series_queries.clear()
        self._log(f"Querying {self.remote_key}...")
        studies_raw = dicom_ops.c_find_studies(study_date=date_range)

        studies = self._filter_studies_by_time(studies_raw, cutoff)
        self._log(f"  {self.remote_key}: {len(studies)} studies in time window")

        study_metadata = self._seed_study_metadata(studies_raw)

        jobs: List[SeriesJob] = []
        for study_ds in studies:
            if self._cancel.is_set():
                break
            jobs.extend(self._build_study_jobs(
                dicom_ops, study_ds, seen_series, max_images,
                study_metadata))

        # Handle prior studies
        if self.config.prior_studies_count > 0:
            prior_jobs = self._resolve_priors(
                dicom_ops, studies, seen_series, max_images)
            jobs.extend(prior_jobs)

        # Reorder so studies whose series descriptions match a
        # configured priority term float to the top of the queue,
        # in the order the terms appear in the source's
        # ``priority_series_terms`` list; within each tier, axial
        # ("ax") series come first across studies and patients, and
        # priors always sink below current studies.  Done BEFORE the
        # ``studies_queried`` emit so the engine's announced
        # work-list and the queue it actually downloads are in the
        # same order (future consumers of ``studies_queried`` won't
        # silently disagree with the queue's order).
        jobs = self._apply_priority_ordering(jobs)

        # Emit study metadata for study rate display.  Covers ALL studies
        # from the query (including filtered-out and time-filtered ones)
        # with InstitutionName patched from series-level where available.
        # Deep-copy so the GUI thread owns the payload independent of the
        # engine's study_metadata dict (which is replaced on each cycle).
        self.signals.studies_queried.emit(
            [dict(v) for v in study_metadata.values()])

        return jobs

    @staticmethod
    def _filter_studies_by_time(studies_raw: List[Dataset],
                                cutoff: datetime) -> List[Dataset]:
        """Keep the studies acquired at or after *cutoff*.

        The C-FIND can only filter by DATE, so the hour-level cutoff is
        applied here.  A study whose StudyDate/StudyTime cannot be
        parsed is KEPT: dropping it would silently skip work because a
        PACS returned a malformed timestamp."""
        studies: List[Dataset] = []
        for s in studies_raw:
            try:
                dt_str = (f"{getattr(s, 'StudyDate', '')}"
                          f"{getattr(s, 'StudyTime', '000000')[:6]}")
                if datetime.strptime(dt_str, '%Y%m%d%H%M%S') >= cutoff:
                    studies.append(s)
            except ValueError:
                studies.append(s)
        return studies

    @staticmethod
    def _seed_study_metadata(
            studies_raw: List[Dataset]) -> Dict[str, dict]:
        """Build the ``studies_queried`` payload from ALL raw query
        results (i.e. BEFORE the time filter), so the dashboard study
        rate sees every study the PACS returned.

        InstitutionName at study level may be empty — it gets patched
        up in _build_study_jobs from the series-level fallback, which
        is why this returns a mutable dict rather than the final
        payload."""
        study_metadata: Dict[str, dict] = {}
        for s in studies_raw:
            uid = getattr(s, 'StudyInstanceUID', '')
            if uid and uid not in study_metadata:
                raw_time = getattr(s, 'StudyTime', '') or ''
                study_metadata[uid] = {
                    "study_uid": uid,
                    "study_date": getattr(s, 'StudyDate', ''),
                    "study_time": raw_time[:6],
                    "institution_name": str(
                        getattr(s, 'InstitutionName', '')).strip(),
                }
        return study_metadata

    def _apply_priority_ordering(
            self, jobs: List[SeriesJob]) -> List[SeriesJob]:
        """Apply this engine's source-level priority list to *jobs*.

        Reads ``priority_series_terms`` from the bound ``PacsNode``
        and delegates the sort to the pure
        :py:meth:`_sort_jobs_by_priority` helper (which doesn't need
        a TransferEngine instance and is therefore easy to test in
        isolation).
        """
        # ``self._remote_node`` raises KeyError if the source was
        # deleted from the config between construction and this
        # cycle's query.  Today the Settings dialog only opens when
        # the engine is stopped, but a future change might loosen
        # that — fall back to "no reorder" instead of crashing the
        # cycle.
        node = self._active_node_or_none
        terms = (list(getattr(node, "priority_series_terms", []))
                 if node is not None else [])
        # No early-out on empty terms: the sort also enforces the
        # axial-first and priors-last tiers, which apply regardless
        # of the configured term list.
        return self._sort_jobs_by_priority(
            jobs, terms, log_label=self.remote_key)

    # The queue-ordering rules live in the pure core.queue_planner
    # module.  These thin staticmethod wrappers preserve the historical
    # TransferEngine._foo(...) call sites and the unit tests that target
    # them directly, while delegating the logic to the testable module.
    @staticmethod
    def _sort_jobs_by_priority(jobs: List[SeriesJob],
                               terms: List[Dict[str, Any]],
                               log_label: str = "") -> List[SeriesJob]:
        return queue_planner.sort_jobs_by_priority(jobs, terms, log_label)

    @staticmethod
    def _first_substantial_series_uids(
            jobs: List[SeriesJob]) -> Set[str]:
        return queue_planner.first_substantial_series_uids(jobs)

    @staticmethod
    def _is_axial(series_description: str) -> bool:
        return queue_planner.is_axial(series_description)

    @staticmethod
    def _compile_matchers(terms: List[Dict[str, Any]],
                          log_label: str = "") -> list:
        return queue_planner.compile_matchers(terms, log_label)

    @staticmethod
    def _compute_study_priorities(
            jobs: List[SeriesJob], matchers: list) -> Dict[str, int]:
        return queue_planner.compute_study_priorities(jobs, matchers)

    def _build_study_jobs(
            self, dicom_ops: DicomOperations, study_ds: Dataset,
            seen_series: Set[str], max_images: int,
            study_metadata: Optional[Dict[str, dict]] = None,
    ) -> List[SeriesJob]:
        """Build SeriesJob items for one study."""
        study_uid = getattr(study_ds, 'StudyInstanceUID', '')
        patient_name = str(getattr(study_ds, 'PatientName', 'Unknown'))
        patient_id = getattr(study_ds, 'PatientID', '')
        study_desc = getattr(study_ds, 'StudyDescription', 'N/A')
        study_date = getattr(study_ds, 'StudyDate', '')
        study_time = getattr(study_ds, 'StudyTime', '')[:6] if getattr(study_ds, 'StudyTime', '') else ''
        accession = getattr(study_ds, 'AccessionNumber', '') or ''
        institution = str(
            getattr(study_ds, 'InstitutionName', '')).strip()

        # Checked variant: a series query that broke off mid-stream
        # returns a SHORT list, and building the queue from it would let
        # _check_study_complete call the study complete on series the
        # PACS never got to enumerate.  Remember the study so the
        # completion decision is skipped for this cycle.
        complete, series_list = dicom_ops.c_find_series_checked(study_uid)
        self._note_series_query_result(study_uid, complete)

        # InstitutionName fallback: read from first series
        if not institution and series_list:
            institution = str(
                getattr(series_list[0], 'InstitutionName', '')).strip()

        # Patch study_metadata with resolved institution (series fallback)
        if study_metadata is not None and study_uid in study_metadata:
            study_metadata[study_uid]["institution_name"] = institution

        institution_ok = self._passes_institution_filter(institution)
        allow_small = (not institution_ok
                       and self.config.filter_allow_small_series)
        if not institution_ok and not allow_small:
            return []

        local_series = self._fetch_local_series_counts(
            dicom_ops, study_uid)

        return self._series_jobs_for(
            series_list,
            seen_series=seen_series,
            local_series=local_series,
            max_images=max_images,
            # Institution filtered out, but the small-series exception
            # lets the little ones through — cap them at the configured
            # size so the exception cannot pull in a whole study.
            max_remote_count=(self.config.filter_small_series_max
                              if allow_small else None),
            patient_name=patient_name,
            patient_id=patient_id,
            study_uid=study_uid,
            study_description=study_desc,
            study_date=study_date,
            study_time=study_time,
            institution_name=institution,
            accession_number=accession,
        )

    def _series_jobs_for(self, series_list: List[Dataset], *,
                         seen_series: Set[str],
                         local_series: Dict[str, int],
                         max_images: int,
                         max_remote_count: Optional[int] = None,
                         **job_fields: Any) -> List[SeriesJob]:
        """Turn a study's series C-FIND results into ``SeriesJob``s.

        The per-series gate — already seen this cycle? already local?
        over the size limit? — is identical for current studies and for
        priors, and only the fields stamped onto the job differ, so those
        come in as *job_fields* and go straight to ``_make_series_job``.

        *max_remote_count*, when given, additionally drops series larger
        than that; only the small-series institution exception uses it.

        Mutates *seen_series*: it is the cycle-wide de-duplication set,
        so a series claimed here must not be claimed again by a later
        study (a prior of one patient can be the current study of
        another).
        """
        jobs: List[SeriesJob] = []
        for ser in series_list:
            series_uid = getattr(ser, 'SeriesInstanceUID', '')
            if series_uid in seen_series:
                continue
            remote_count = int(
                getattr(ser, 'NumberOfSeriesRelatedInstances', 0) or 0)
            local_count = local_series.get(series_uid, 0)
            if self._should_skip_series(
                    remote_count, local_count, max_images):
                continue
            if (max_remote_count is not None
                    and remote_count > max_remote_count):
                continue
            seen_series.add(series_uid)
            jobs.append(self._make_series_job(
                ser,
                remote_count=remote_count,
                local_count=local_count,
                **job_fields,
            ))
        return jobs

    @staticmethod
    def _series_datetime(ser: Dataset, study_date: str,
                         study_time: str) -> Tuple[str, str]:
        """Return ``(series_date, series_time)`` for a series C-FIND
        result, falling back to the study's date/time when the PACS
        omits the series-level values.  ``series_time`` is trimmed to
        ``HHMMSS`` (DICOM TM may carry fractional seconds)."""
        s_date = str(getattr(ser, 'SeriesDate', '') or '').strip()
        s_time_raw = str(getattr(ser, 'SeriesTime', '') or '').strip()
        s_time = s_time_raw[:6]
        return (s_date or study_date, s_time or study_time)

    def _make_series_job(self, ser: Dataset, *, remote_count: int,
                         local_count: int, patient_name: str,
                         patient_id: str, study_uid: str,
                         study_description: str, study_date: str,
                         study_time: str, institution_name: str = "",
                         accession_number: str = "",
                         is_prior: bool = False) -> SeriesJob:
        """Build one ``SeriesJob`` from a series C-FIND result.

        Shared by the current-study and prior-study queue builders, which
        otherwise duplicated this whole construction (series date/time
        resolution, blacklist→"unavailable" status, field mapping).  The
        differing bits (study_description prefix, institution/accession,
        is_prior) are passed in by the caller."""
        series_uid = getattr(ser, 'SeriesInstanceUID', '')
        ser_date, ser_time = self._series_datetime(
            ser, study_date, study_time)
        # Series that already failed MAX_SERIES_TRANSFER_ATTEMPTS times
        # stay visible in the queue with an "unavailable" status instead
        # of being re-attempted forever — small series often fail to
        # transfer for DICOM-level reasons.
        blacklisted = self._transfer_log.is_series_blacklisted(
            source_pacs=self.remote_key,
            series_uid=series_uid,
            max_attempts=MAX_SERIES_TRANSFER_ATTEMPTS)
        return SeriesJob(
            patient_name=patient_name,
            patient_id=patient_id,
            study_description=study_description,
            series_description=getattr(ser, 'SeriesDescription', 'N/A'),
            modality=getattr(ser, 'Modality', 'UN'),
            series_number=str(getattr(ser, 'SeriesNumber', '')),
            study_uid=study_uid,
            series_uid=series_uid,
            remote_count=remote_count,
            local_count=local_count,
            institution_name=institution_name,
            accession_number=accession_number,
            study_date=study_date,
            study_time=study_time,
            series_date=ser_date,
            series_time=ser_time,
            is_prior=is_prior,
            status="unavailable" if blacklisted else "queued",
        )

    def _transfer_series(self, job: SeriesJob,
                         dicom_ops: Optional[DicomOperations] = None
                         ) -> int:
        """Transfer one series. Returns number of images transferred."""
        job.status = "transferring"
        self.signals.series_started.emit(job.to_dict())

        to_transfer = job.to_transfer
        self._log(f"[{self.remote_key}] Downloading: {job.patient_name} — "
                  f"[{job.modality}] {job.series_description} "
                  f"({to_transfer} images)")

        if self._cancel.is_set():
            job.status = "error"
            return 0

        dicom_ops = self._ensure_dicom_ops(dicom_ops)

        success, images, t_elapsed = self._do_move(dicom_ops, job)
        if success:
            return self._record_success(job, images, t_elapsed,
                                        to_transfer)
        return self._record_failure(job, t_elapsed)

    def _do_move(self, dicom_ops: DicomOperations,
                 job: SeriesJob) -> Tuple[bool, int, float]:
        """Run the C-MOVE.  Returns (success, images, elapsed).

        Emits ``series_progress`` so the GUI's stall watchdog sees the
        transfer is alive (it resets its no-progress clock) and the live
        Pending/ETE counters advance.  The emit is THROTTLED to one every
        ``PROGRESS_EMIT_INTERVAL_S``: pynetdicom yields a status per
        image, and emitting a queued cross-thread signal for each image
        of a large series floods the GUI event queue and freezes the UI.
        The final image (completed >= total) always emits so the bar
        finishes at 100%."""
        t_start = time.time()
        last_emit = [0.0]  # list = mutable cell for the closure

        def _on_progress(completed: int, total: int) -> None:
            now = time.time()
            is_final = total > 0 and completed >= total
            if not is_final and (now - last_emit[0]
                                 < PROGRESS_EMIT_INTERVAL_S):
                return
            last_emit[0] = now
            try:
                self.signals.series_progress.emit(
                    job.series_uid, completed, total)
            except RuntimeError:
                # Signals QObject torn down during shutdown — see _log.
                pass

        try:
            success, images = dicom_ops.c_move_series(
                job.study_uid, job.series_uid, progress_cb=_on_progress)
        except PacsConnectionError:
            # PACS unreachable — let _transfer_queue abort the cycle
            # and raise the one-per-outage connection_lost popup.
            raise
        except Exception as e:
            self._log(f"  [{self.remote_key}] C-MOVE failed: {e}")
            return False, 0, time.time() - t_start
        return success, images, time.time() - t_start

    def _add_study_active_time(self, study_uid: str,
                               seconds: float) -> None:
        """Accumulate transfer time spent on one of *study_uid*'s own
        series.  Queue order interleaves studies (axial fast-lane), so
        the study wall clock must count only its own series' time."""
        self._study_active_seconds[study_uid] = (
            self._study_active_seconds.get(study_uid, 0.0) + seconds)

    def _record_success(self, job: SeriesJob, images: int,
                        t_elapsed: float,
                        to_transfer: int) -> int:
        """Update stats, persist, and emit completion signals.

        *images* is the raw number of completed sub-operations the source
        PACS reported.  It is rounded UP to *to_transfer* for stats,
        display and the transfer log (an SCP that under-reports would
        otherwise make a full series look partial), but the raw value is
        what feeds ``_study_images_transferred`` — the completion
        timestamp may only move when images demonstrably arrived, and a
        C-MOVE that answers "success" without moving a single image must
        not be inflated into a full transfer there.
        """
        reported = max(images, 0)
        images = max(images, to_transfer)
        self.stats.record_series(job.series_uid, images, t_elapsed)
        ipm = (images / t_elapsed) * 60 if t_elapsed > 0 else 0.0
        job.images_per_minute = ipm
        job.transferred_images = images
        job.duration_seconds = t_elapsed
        job.status = "done"
        self._add_study_active_time(job.study_uid, t_elapsed)
        self._persist_series_record(job, images, t_elapsed)
        if reported:
            self._study_images_transferred[job.study_uid] = (
                self._study_images_transferred.get(job.study_uid, 0)
                + reported)
        self.signals.series_completed.emit(job.series_uid, images)
        self.signals.stats_updated.emit(self.stats)
        self._check_study_complete(job.study_uid)
        self._check_patient_complete(job.patient_id)
        return images

    def _persist_series_record(self, job: SeriesJob, images: int,
                               t_elapsed: float) -> None:
        """Log the finished series to SQLite and clear its failure
        streak.  Mirrors :py:meth:`_persist_study_record` at series
        level.

        *images* is the rounded-up count (see _record_success): the log
        records what the series is worth, not what a possibly
        under-reporting SCP claimed.  Both writes are best-effort — a
        broken transfer log must never abort a transfer that already
        succeeded on the wire — hence the per-statement sqlite3.Error
        guards, and clearing the failures even if the record write
        failed."""
        try:
            self._transfer_log.record_series(
                source_pacs=self.remote_key,
                study_uid=job.study_uid,
                series_uid=job.series_uid,
                patient_id=job.patient_id,
                accession_number=job.accession_number,
                study_date=job.study_date,
                study_time=job.study_time,
                modality=job.modality,
                study_description=job.study_description,
                series_description=job.series_description,
                series_number=job.series_number,
                image_count=images,
                duration_seconds=t_elapsed,
            )
        except sqlite3.Error as e:
            logger.warning(f"TransferLog.record_series failed: {e}")
        try:
            self._transfer_log.clear_series_failures(
                source_pacs=self.remote_key,
                series_uid=job.series_uid)
        except sqlite3.Error as e:
            logger.warning(
                f"TransferLog.clear_series_failures failed: {e}")

    def _record_failure(self, job: SeriesJob,
                        t_elapsed: float = 0.0) -> int:
        """Mark the job as errored, persist the failure, and emit.

        *t_elapsed* (time spent on the failed attempt) still counts
        toward the study's wall clock — the engine was busy with this
        study for that long."""
        job.status = "error"
        self._add_study_active_time(job.study_uid, t_elapsed)
        try:
            self._transfer_log.record_series_failure(
                source_pacs=self.remote_key,
                series_uid=job.series_uid)
        except sqlite3.Error as e:
            logger.warning(
                f"TransferLog.record_series_failure failed: {e}")
        self.signals.series_error.emit(
            job.series_uid, "Transfer failed")
        return 0

    def _check_study_complete(self, study_uid: str):
        """Emit study_completed once all series of a study are terminal.

        Thin orchestrator: detect completion → persist the study record →
        decide fully_complete → emit.

        Re-emits on later completions only when more than
        MIN_IMAGES_TO_REFRESH_COMPLETION further images have actually
        ARRIVED since the last emit (a late series, or a small series
        that finally crossed).  That advances the Download Completions
        row's timestamp to the latest image arrival and re-fires the
        completion chime.  Everything else stays silent:

        * failed transfer attempts — they never reach _record_success;
        * a "successful" C-MOVE that moved zero images;
        * straggler waves of 1–5 images (retries / re-sends);
        * a new CYCLE re-attempting the same stragglers — the arrival
          counter and the last-emitted mark both survive cycles.

        The FIRST completion of a study always fires, so a study that
        finishes in one go still gets its Download Completions row."""
        # A study whose series C-FIND broke off mid-stream is only
        # partially represented in the queue, so "all series terminal"
        # would be a verdict about series we never saw.  Stay silent and
        # let the next cycle — which re-queries the study — decide.
        # Bounded: after MAX_INCOMPLETE_QUERY_CYCLES consecutive
        # truncations we accept the partial view rather than withhold
        # the study forever.
        if self._completion_deferred(study_uid):
            return
        # Read the queue under the lock, per the class contract (see the
        # _queue_lock docstring): whole-list reassignment is guarded, so
        # snapshotting here keeps this consistent with that invariant
        # even if a future caller runs it off the service thread.
        with self._queue_lock:
            study_series = [j for j in self._queue
                            if j.study_uid == study_uid]
        if not study_series:
            return
        if not all(j.status in TERMINAL_STATUSES for j in study_series):
            return

        done_series = [j for j in study_series if j.status == "done"]
        # Per-cycle aggregate — what the transfer log records and what the
        # GUI shows for this wave.
        total_images = sum(j.transferred_images for j in done_series)
        # Lifetime aggregate — the only basis for the refresh decision.
        arrived = self._study_images_transferred.get(study_uid, 0)
        last_emitted = self._completed_studies.get(study_uid)
        if last_emitted is not None:
            # Already signalled once: only refresh when a real wave of new
            # images arrived.  A handful of straggler images (retries /
            # re-sends) must not advance the completion time.
            if arrived - last_emitted <= MIN_IMAGES_TO_REFRESH_COMPLETION:
                return
        self._completed_studies[study_uid] = arrived
        institution = study_series[0].institution_name
        self._persist_study_record(study_uid, done_series, total_images)

        fully_complete = self._compute_fully_complete(study_uid, study_series)
        self.signals.study_completed.emit(
            study_uid, institution, fully_complete, total_images)

    def _persist_study_record(self, study_uid: str,
                              done_series: List[SeriesJob],
                              total_images: int) -> None:
        """Log the study-level aggregate to SQLite and stash its
        wall-clock duration for the GUI to pop.  No-op when no series of
        the study actually completed."""
        if not done_series:
            return
        first = done_series[0]
        total_duration = sum(j.duration_seconds for j in done_series)
        # Accumulated time spent on this study's own series (incl. failed
        # attempts).  NOT first-start-to-last-end: the axial fast-lane
        # interleaves studies in the queue, so that span would include
        # other studies' downloads.
        wall_clock = self._study_active_seconds.pop(study_uid, total_duration)
        try:
            self._transfer_log.record_study(
                source_pacs=self.remote_key,
                study_uid=study_uid,
                patient_id=first.patient_id,
                accession_number=first.accession_number,
                study_date=first.study_date,
                study_time=first.study_time,
                modality=first.modality,
                study_description=first.study_description,
                total_series=len(done_series),
                total_images=total_images,
                total_duration_seconds=total_duration,
                wall_clock_seconds=wall_clock,
            )
        except sqlite3.Error as e:
            logger.warning(f"TransferLog.record_study failed: {e}")
        with self._study_wall_clock_lock:
            self._study_wall_clock[study_uid] = wall_clock

    def _compute_fully_complete(self, study_uid: str,
                                study_series: List[SeriesJob]) -> bool:
        """Whether the study counts as fully complete.

        Requires every queued series to be ``done``, except small series
        (< SMALL_SERIES_MAX_IMAGES_FOR_COMPLETION remote images) and
        "unavailable" series (retry budget exhausted, can never arrive)
        may also be error/skipped without blocking.  Filter-rejected
        series are not in study_series at all.

        ``fully_complete`` drives BOTH the completion chime and the
        Download Completions row, and the user wants both to fire on
        every completion — so there is deliberately NO cross-cycle
        de-dup here.  A no-op re-query of an already-local study produces
        no ``done`` series, so the GUI handlers (which require done
        series) naturally skip it without needing a dedup flag."""
        def _ok_for_completion(j: SeriesJob) -> bool:
            if j.status in ("done", "unavailable"):
                return True
            return j.remote_count < SMALL_SERIES_MAX_IMAGES_FOR_COMPLETION

        return all(_ok_for_completion(j) for j in study_series)

    def _check_patient_complete(self, patient_id: str):
        """Emit patient_studies_completed if all series for this patient
        (including priors) are done or errored."""
        if patient_id in self._completed_patients:
            return
        # Read the queue under the lock, per the class contract — see
        # _check_study_complete for the rationale.
        with self._queue_lock:
            patient_series = [j for j in self._queue
                              if j.patient_id == patient_id]
        if not patient_series:
            return
        if all(j.status in TERMINAL_STATUSES
               for j in patient_series):
            self._completed_patients.add(patient_id)
            institution = patient_series[0].institution_name
            self.signals.patient_studies_completed.emit(
                patient_id, institution)

    def _resolve_priors(self, dicom_ops: DicomOperations,
                        current_studies: List[Dataset],
                        seen_series: Set[str],
                        max_images: int) -> List[SeriesJob]:
        """Find prior studies for the patients in current_studies."""
        prior_jobs: List[SeriesJob] = []
        patients_done: Set[str] = set()

        for study_ds in current_studies:
            pid = getattr(study_ds, 'PatientID', '')
            if not pid or pid in patients_done:
                continue
            patients_done.add(pid)
            prior_jobs.extend(self._resolve_priors_for_patient(
                dicom_ops, pid, current_studies, seen_series, max_images))

        return prior_jobs

    def _resolve_priors_for_patient(
            self, dicom_ops: DicomOperations, pid: str,
            current_studies: List[Dataset], seen_series: Set[str],
            max_images: int) -> List[SeriesJob]:
        """Resolve the prior-study series jobs for a single patient."""
        prior_studies = self._find_prior_candidates(
            dicom_ops, pid, current_studies)
        prior_studies = self._top_n_priors(
            prior_studies, current_studies, pid)

        jobs: List[SeriesJob] = []
        for ps in prior_studies:
            jobs.extend(self._build_prior_jobs_for_study(
                dicom_ops, ps, pid, seen_series, max_images))

        if jobs:
            self._log(f"  {len(jobs)} prior series for patient {pid}")
        return jobs

    def _find_prior_candidates(self,
                               dicom_ops: DicomOperations,
                               pid: str,
                               current_studies: List[Dataset]
                               ) -> List[Dataset]:
        """Query the PACS for the patient's full study history and
        drop the studies already covered by the current cycle's time
        window."""
        current_uids = {getattr(s, 'StudyInstanceUID', '')
                        for s in current_studies
                        if getattr(s, 'PatientID', '') == pid}
        all_raw = dicom_ops.c_find_studies(patient_id=pid)
        self._log(f"  [Prior] patient {pid}: {len(all_raw)} total studies on PACS, "
                  f"{len(current_uids)} in current window")
        prior_studies = [s for s in all_raw
                         if getattr(s, 'StudyInstanceUID', '') not in current_uids]
        self._log(f"  [Prior] {len(prior_studies)} candidate prior studies")
        return prior_studies

    def _top_n_priors(self, prior_studies: List[Dataset],
                      current_studies: List[Dataset],
                      pid: str) -> List[Dataset]:
        """Sort newest-first, optionally filter to matching modality,
        then truncate to the configured count."""
        # ``sorted(...)`` returns a fresh list so future callers can
        # reuse ``prior_studies`` without seeing it mutated under them.
        prior_studies = sorted(
            prior_studies,
            key=lambda x: (getattr(x, 'StudyDate', ''),
                           getattr(x, 'StudyTime', '')),
            reverse=True)

        if self.config.prior_studies_same_modality:
            prior_studies = self._filter_priors_by_modality(
                prior_studies, current_studies, pid)

        count = min(self.config.prior_studies_count, len(prior_studies))
        self._log(f"  [Prior] downloading {count} of {len(prior_studies)} "
                  f"(configured max: {self.config.prior_studies_count})")
        return prior_studies[:count]

    def _filter_priors_by_modality(
            self, prior_studies: List[Dataset],
            current_studies: List[Dataset],
            pid: str) -> List[Dataset]:
        """Keep only prior studies whose modality set intersects the
        modalities of the current studies for this patient."""
        def split_modalities(ds: Dataset) -> Set[str]:
            return {m.strip()
                    for m in str(getattr(ds, 'ModalitiesInStudy', ''))
                    .replace("\\", ",").split(",")
                    if m.strip()}

        target_mods: Set[str] = set()
        for cs in current_studies:
            if getattr(cs, 'PatientID', '') == pid:
                target_mods |= split_modalities(cs)
        self._log(f"  [Prior] modality filter active, target: {target_mods}")
        if not target_mods:
            return prior_studies
        kept = [s for s in prior_studies
                if split_modalities(s) & target_mods]
        self._log(f"  [Prior] {len(kept)} after modality filter")
        return kept

    def _build_prior_jobs_for_study(
            self, dicom_ops: DicomOperations, ps: Dataset, pid: str,
            seen_series: Set[str], max_images: int) -> List[SeriesJob]:
        """Build SeriesJob items for one prior study, applying the
        institution filter and seen/local checks."""
        ps_uid = getattr(ps, 'StudyInstanceUID', '')
        ps_name = str(getattr(ps, 'PatientName', 'Unknown'))
        ps_desc = getattr(ps, 'StudyDescription', 'N/A')
        ps_date = getattr(ps, 'StudyDate', '')
        ps_time_raw = getattr(ps, 'StudyTime', '')
        ps_time = ps_time_raw[:6] if ps_time_raw else ''

        institution = str(getattr(ps, 'InstitutionName', '')).strip()
        # See _build_study_jobs: priors reach _check_study_complete the
        # same way current studies do, so a truncated series list must
        # suppress their completion verdict too.
        complete, series_list = dicom_ops.c_find_series_checked(ps_uid)
        if not complete:
            self._note_series_query_result(ps_uid, False)
        if not institution and series_list:
            institution = str(
                getattr(series_list[0], 'InstitutionName', '')).strip()
        if not self._passes_institution_filter(institution):
            return []
        local_series = self._fetch_local_series_counts(dicom_ops, ps_uid)

        return self._series_jobs_for(
            series_list,
            seen_series=seen_series,
            local_series=local_series,
            max_images=max_images,
            patient_name=ps_name,
            patient_id=pid,
            study_uid=ps_uid,
            study_description=f"[Prior] {ps_desc}",
            study_date=ps_date,
            study_time=ps_time,
            is_prior=True,
        )

    # ── Institution filter logic ──────────────────────────────────────────

    def _passes_institution_filter(self, institution_name: str) -> bool:
        """
        Check whether a study from the given institution should be downloaded.

        Rules (when filtering is enabled):
        - If institution is assigned to an active group → download
        - If institution is assigned to an inactive group → skip
        - If institution is unknown (not assigned to any group) → download
          AND emit unknown_institution signal so the GUI can alert the user
        - If filtering is disabled → always download
        """
        if not self.config.filter_groups_enabled:
            return True

        active_groups = set(self.config.active_filter_groups)
        if not active_groups:
            # No groups selected = no filtering active
            return True

        assignments = self.config.institution_assignments
        assigned_group = assignments.get(institution_name, "")

        if not assigned_group:
            # Unknown / unassigned institution → download + alert
            if institution_name and institution_name not in self._notified_institutions:
                self._notified_institutions.add(institution_name)
                self.signals.unknown_institution.emit(institution_name)
            return True

        # Known institution: check if its group is active
        return assigned_group in active_groups

    # ── Reusable helpers ──────────────────────────────────────────────

    @staticmethod
    def _should_skip_series(remote_count: int, local_count: int,
                            max_images: int) -> bool:
        """Return True if a series does not need to be transferred."""
        if remote_count == 0 or local_count >= remote_count:
            return True
        if max_images > 0 and remote_count > max_images:
            return True
        missing = remote_count - local_count
        # Tolerate 1-2 missing images in larger series — PACS counts
        # can fluctuate due to pending storage commits or routing delays.
        if remote_count > 10 and missing <= 2:
            return True
        return False

    @staticmethod
    def _fetch_local_series_counts(
            dicom_ops: DicomOperations, study_uid: str) -> Dict[str, int]:
        """Query local PACS and return {series_uid: image_count}.

        Returns an empty dict on failure. Failures are logged at WARNING
        because a swallowed local-query error makes the engine think
        nothing is locally stored, causing it to re-download the entire
        study every cycle until the local PACS recovers.
        """
        counts: Dict[str, int] = {}
        try:
            for ls in dicom_ops.c_find_local_series(study_uid):
                uid = getattr(ls, 'SeriesInstanceUID', '')
                cnt = int(
                    getattr(ls, 'NumberOfSeriesRelatedInstances', 0) or 0)
                if uid:
                    counts[uid] = cnt
        except Exception as e:
            logger.warning(
                f"Local PACS query failed for study {study_uid}: {e} — "
                f"engine will treat the study as empty locally and may "
                f"re-download series until the local PACS recovers")
        return counts

    def _make_dicom_ops(self) -> DicomOperations:
        remote_node = self.config.remote_nodes[self.remote_key]
        local_config = self.config.get_local_dict_for(self.remote_key)
        return DicomOperations(
            local_config,
            remote_node.to_dict(),
            self.remote_key,
        )
