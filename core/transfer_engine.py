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

from core.dicom_ops import DicomOperations
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
    status: str = "queued"  # queued, transferring, done, error, skipped
    institution_name: str = ""
    accession_number: str = ""
    images_per_minute: float = 0.0
    transferred_images: int = 0
    duration_seconds: float = 0.0
    study_date: str = ""
    study_time: str = ""

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
            "study_date": self.study_date,
            "study_time": self.study_time,
        }


@dataclass
class SeriesCompletionRecord:
    """Stores the measured speed for one completed series."""
    series_uid: str = ""
    image_count: int = 0
    duration_seconds: float = 0.0
    images_per_minute: float = 0.0


# Series with fewer than this many remote images may fail (error/
# skipped) without blocking the study's fully_complete signal — small
# series are typically localizers or stuck tiny series that should not
# silently suppress the Download Completions entry for everything else
# that did arrive.
SMALL_SERIES_MAX_IMAGES_FOR_COMPLETION = 6


@dataclass
class TransferStats:
    """Per-series throughput statistics with median aggregation.

    Only series with at least ``MIN_IMAGES_FOR_STATS`` images are
    included in the speed statistics.  Smaller series are still counted
    towards ``total_images`` but their transfer speed is too noisy to
    be meaningful.
    """
    MIN_IMAGES_FOR_STATS: ClassVar[int] = 10

    total_images: int = 0
    start_time: float = 0.0
    _completed_series: List["SeriesCompletionRecord"] = field(
        default_factory=list)

    def start_session(self):
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
        self.total_images += image_count
        ipm = (image_count / duration_seconds) * 60 if duration_seconds > 0 else 0.0
        self._completed_series.append(SeriesCompletionRecord(
            series_uid=series_uid,
            image_count=image_count,
            duration_seconds=duration_seconds,
            images_per_minute=ipm,
        ))

    @property
    def completed_count(self) -> int:
        return len(self._completed_series)

    @property
    def _stats_series(self) -> List["SeriesCompletionRecord"]:
        """Completed series that qualify for speed statistics."""
        return [r for r in self._completed_series
                if r.image_count >= self.MIN_IMAGES_FOR_STATS]

    def raw_images_per_minute(self) -> float:
        """Overall images/minute since session start.

        Reads ``start_time`` and ``total_images`` in a single snapshot
        so callers on another thread get a consistent rate."""
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
        qualifying = self._stats_series
        if not qualifying:
            return 0.0
        return qualifying[-1].images_per_minute

    @staticmethod
    def _median(values: List[float]) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        n = len(s)
        mid = n // 2
        if n % 2 == 1:
            return s[mid]
        return (s[mid - 1] + s[mid]) / 2

    def median_n_ipm(self, n: int) -> float:
        """Median images/minute over the last *n* qualifying series."""
        qualifying = self._stats_series
        if not qualifying:
            return 0.0
        recent = qualifying[-n:]
        return self._median([r.images_per_minute for r in recent])

    def median_all_ipm(self) -> float:
        """Median images/minute across all qualifying series."""
        return self._median(
            [r.images_per_minute for r in self._stats_series])

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
    study_completed = Signal(str, str, bool)  # study_uid, institution_name, fully_complete


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
        self._completed_patients: Set[str] = set()
        self._completed_studies: Set[str] = set()
        self._transfer_log = (transfer_log if transfer_log is not None
                              else TransferLog(default_db_path()))
        self._series_start_times: Dict[str, float] = {}  # study_uid → first series start
        self._study_wall_clock: Dict[str, float] = {}  # study_uid → wall-clock seconds, populated at completion
        self._study_wall_clock_lock = threading.Lock()
        self._dicom_ops: Optional[DicomOperations] = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def _remote_node(self):
        return self.config.remote_nodes[self.remote_key]

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
        self.signals.log_message.emit(msg)

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
            if ops is not None and ops.ae is not None:
                try:
                    ops.ae.shutdown()
                except Exception as e:
                    logger.warning(
                        f"[{self.remote_key}] AE shutdown failed: {e}")
            self._dicom_ops = None
            self._running = False
            self._log(f"[{self.remote_key}] Service stopped.")
            self.signals.service_stopped.emit()

    def _run_one_cycle(self, hours: int, max_images: int) -> int:
        """Query the source PACS, build queue, transfer everything."""
        self._completed_patients.clear()
        self._completed_studies.clear()

        # Reuse a single DicomOperations (= one pynetdicom AE) for the
        # lifetime of the engine.  Each AE spawns reactor threads that
        # outlive the Python object; letting the AE be GC'd while those
        # threads are still running causes a SIGSEGV in mark_stacks.
        if self._dicom_ops is None:
            self._dicom_ops = self._make_dicom_ops()
        dicom_ops = self._dicom_ops

        jobs = self._query_and_build_queue(hours, max_images, dicom_ops)
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

    def _query_and_build_queue(self, hours: int, max_images: int,
                               dicom_ops: DicomOperations) -> List[SeriesJob]:
        """Run the C-FIND pass and produce the list of series to transfer."""
        now = datetime.now()
        cutoff = now - timedelta(hours=hours)
        yesterday = now - timedelta(days=1)
        date_range = f"{yesterday.strftime('%Y%m%d')}-{now.strftime('%Y%m%d')}"
        seen_series: Set[str] = set()

        try:
            jobs = self._query_source(
                date_range, cutoff, max_images, seen_series, dicom_ops)
        except Exception as e:
            self._log(f"  [{self.remote_key}] Error querying: {e}")
            jobs = []

        if not jobs:
            with self._queue_lock:
                self._queue = []
            self.signals.queue_updated.emit([])
        return jobs

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
        """Transfer every job in the queue; return total images moved."""
        total_images = 0
        for job in jobs:
            if self._cancel.is_set():
                break
            total_images += self._transfer_series(job, dicom_ops)
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
        """Query the source PACS and return new SeriesJob items."""
        dicom_ops = self._ensure_dicom_ops(dicom_ops)
        self._log(f"Querying {self.remote_key}...")
        studies_raw = dicom_ops.c_find_studies(study_date=date_range)

        # Filter by time
        studies = []
        for s in studies_raw:
            try:
                dt_str = (f"{getattr(s, 'StudyDate', '')}"
                          f"{getattr(s, 'StudyTime', '000000')[:6]}")
                if datetime.strptime(dt_str, '%Y%m%d%H%M%S') >= cutoff:
                    studies.append(s)
            except ValueError:
                studies.append(s)

        self._log(f"  {self.remote_key}: {len(studies)} studies in time window")

        # Seed study_metadata from ALL raw query results (before time
        # filter) so the dashboard study rate sees every study the PACS
        # returned.  InstitutionName at study level may be empty — it
        # gets patched up in _build_study_jobs from the series fallback.
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

        # Emit study metadata for study rate display.  Covers ALL studies
        # from the query (including filtered-out and time-filtered ones)
        # with InstitutionName patched from series-level where available.
        # Deep-copy so the GUI thread owns the payload independent of the
        # engine's study_metadata dict (which is replaced on each cycle).
        self.signals.studies_queried.emit(
            [dict(v) for v in study_metadata.values()])

        return jobs

    def _build_study_jobs(
            self, dicom_ops: DicomOperations, study_ds,
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

        series_list = dicom_ops.c_find_series(study_uid)

        # Exclude blacklisted series: they will never be fetched, so
        # they must not appear in the queue.
        series_list = [
            ser for ser in series_list
            if not self._transfer_log.is_series_blacklisted(
                source_pacs=self.remote_key,
                series_uid=getattr(ser, 'SeriesInstanceUID', ''))
        ]

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

        small_max = self.config.filter_small_series_max
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

            # Institution filtered but small-series exception applies
            if allow_small and remote_count > small_max:
                continue

            seen_series.add(series_uid)
            jobs.append(SeriesJob(
                patient_name=patient_name,
                patient_id=patient_id,
                study_description=study_desc,
                series_description=getattr(ser, 'SeriesDescription', 'N/A'),
                modality=getattr(ser, 'Modality', 'UN'),
                series_number=str(getattr(ser, 'SeriesNumber', '')),
                study_uid=study_uid,
                series_uid=series_uid,
                remote_count=remote_count,
                local_count=local_count,
                institution_name=institution,
                accession_number=accession,
                study_date=study_date,
                study_time=study_time,
            ))
        return jobs

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

        success, images, t_elapsed, t_start = self._do_move(dicom_ops, job)
        if success:
            return self._record_success(job, images, t_elapsed, t_start,
                                        to_transfer)
        return self._record_failure(job)

    def _do_move(self, dicom_ops: DicomOperations,
                 job: SeriesJob) -> Tuple[bool, int, float, float]:
        """Run the C-MOVE.  Returns (success, images, elapsed, t_start)."""
        t_start = time.time()
        try:
            success, images = dicom_ops.c_move_series(
                job.study_uid, job.series_uid)
        except Exception as e:
            self._log(f"  [{self.remote_key}] C-MOVE failed: {e}")
            return False, 0, time.time() - t_start, t_start
        return success, images, time.time() - t_start, t_start

    def _record_success(self, job: SeriesJob, images: int,
                        t_elapsed: float, t_start: float,
                        to_transfer: int) -> int:
        """Update stats, persist, and emit completion signals."""
        images = max(images, to_transfer)
        self.stats.record_series(job.series_uid, images, t_elapsed)
        ipm = (images / t_elapsed) * 60 if t_elapsed > 0 else 0.0
        job.images_per_minute = ipm
        job.transferred_images = images
        job.duration_seconds = t_elapsed
        job.status = "done"
        if job.study_uid not in self._series_start_times:
            self._series_start_times[job.study_uid] = t_start
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
        self.signals.series_completed.emit(job.series_uid, images)
        self.signals.stats_updated.emit(self.stats)
        self._check_study_complete(job.study_uid)
        self._check_patient_complete(job.patient_id)
        return images

    def _record_failure(self, job: SeriesJob) -> int:
        """Mark the job as errored, persist the failure, and emit."""
        job.status = "error"
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
        """Emit study_completed when all series of a study are done."""
        if study_uid in self._completed_studies:
            return
        study_series = [j for j in self._queue
                        if j.study_uid == study_uid]
        if not study_series:
            return
        if all(j.status in ("done", "error", "skipped")
               for j in study_series):
            self._completed_studies.add(study_uid)
            institution = study_series[0].institution_name
            # Log study-level aggregate to SQLite
            done_series = [j for j in study_series if j.status == "done"]
            if done_series:
                first = done_series[0]
                total_images = sum(j.transferred_images for j in done_series)
                total_duration = sum(j.duration_seconds for j in done_series)
                wall_start = self._series_start_times.get(
                    study_uid, time.time())
                wall_clock = time.time() - wall_start
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
                self._series_start_times.pop(study_uid, None)
                with self._study_wall_clock_lock:
                    self._study_wall_clock[study_uid] = wall_clock
            # fully_complete: every queued series of this study must be
            # ``done``, except small series (< SMALL_SERIES_MAX_IMAGES…
            # remote images) may also be error/skipped without blocking.
            # Filter-rejected series are not in study_series at all, so
            # they're naturally excluded.

            def _ok_for_completion(j: SeriesJob) -> bool:
                if j.status == "done":
                    return True
                return j.remote_count < SMALL_SERIES_MAX_IMAGES_FOR_COMPLETION

            fully_complete = all(_ok_for_completion(j)
                                 for j in study_series)
            self.signals.study_completed.emit(
                study_uid, institution, fully_complete)

    def _check_patient_complete(self, patient_id: str):
        """Emit patient_studies_completed if all series for this patient
        (including priors) are done or errored."""
        if patient_id in self._completed_patients:
            return
        patient_series = [j for j in self._queue
                          if j.patient_id == patient_id]
        if not patient_series:
            return
        if all(j.status in ("done", "error", "skipped")
               for j in patient_series):
            self._completed_patients.add(patient_id)
            institution = patient_series[0].institution_name
            self.signals.patient_studies_completed.emit(
                patient_id, institution)

    def _resolve_priors(self, dicom_ops: DicomOperations,
                        current_studies, seen_series: Set[str],
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
            current_studies, seen_series: Set[str],
            max_images: int) -> List[SeriesJob]:
        """Resolve the prior-study series jobs for a single patient."""
        current_uids = {getattr(s, 'StudyInstanceUID', '')
                        for s in current_studies
                        if getattr(s, 'PatientID', '') == pid}

        all_raw = dicom_ops.c_find_studies(patient_id=pid)
        self._log(f"  [Prior] patient {pid}: {len(all_raw)} total studies on PACS, "
                  f"{len(current_uids)} in current window")
        prior_studies = [s for s in all_raw
                         if getattr(s, 'StudyInstanceUID', '') not in current_uids]
        self._log(f"  [Prior] {len(prior_studies)} candidate prior studies")

        prior_studies.sort(
            key=lambda x: (getattr(x, 'StudyDate', ''),
                           getattr(x, 'StudyTime', '')),
            reverse=True)

        if self.config.prior_studies_same_modality:
            prior_studies = self._filter_priors_by_modality(
                prior_studies, current_studies, pid)

        count = min(self.config.prior_studies_count, len(prior_studies))
        self._log(f"  [Prior] downloading {count} of {len(prior_studies)} "
                  f"(configured max: {self.config.prior_studies_count})")

        jobs: List[SeriesJob] = []
        for ps in prior_studies[:count]:
            jobs.extend(self._build_prior_jobs_for_study(
                dicom_ops, ps, pid, seen_series, max_images))

        if jobs:
            self._log(f"  {len(jobs)} prior series for patient {pid}")
        return jobs

    def _filter_priors_by_modality(
            self, prior_studies, current_studies, pid: str):
        """Keep only prior studies whose modality set intersects the
        modalities of the current studies for this patient."""
        def split_modalities(ds) -> Set[str]:
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
            self, dicom_ops: DicomOperations, ps, pid: str,
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
        series_list = dicom_ops.c_find_series(ps_uid)
        if not institution and series_list:
            institution = str(
                getattr(series_list[0], 'InstitutionName', '')).strip()
        if not self._passes_institution_filter(institution):
            return []
        local_series = self._fetch_local_series_counts(dicom_ops, ps_uid)

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
            seen_series.add(series_uid)
            jobs.append(SeriesJob(
                patient_name=ps_name,
                patient_id=pid,
                study_description=f"[Prior] {ps_desc}",
                series_description=getattr(ser, 'SeriesDescription', 'N/A'),
                modality=getattr(ser, 'Modality', 'UN'),
                series_number=str(getattr(ser, 'SeriesNumber', '')),
                study_uid=ps_uid,
                series_uid=series_uid,
                remote_count=remote_count,
                local_count=local_count,
                study_date=ps_date,
                study_time=ps_time,
            ))
        return jobs

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
