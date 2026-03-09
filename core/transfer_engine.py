"""
Transfer engine for DICOM Sync GUI.
Runs a continuous service loop: query → compare → transfer → wait → repeat.
Emits Qt signals so the GUI can display queue and progress in real time.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QObject, Signal

from core.dicom_ops import DicomOperations

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
    images_per_minute: float = 0.0

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
            "images_per_minute": self.images_per_minute,
        }


@dataclass
class SeriesCompletionRecord:
    """Stores the measured speed for one completed series."""
    series_uid: str = ""
    image_count: int = 0
    duration_seconds: float = 0.0
    images_per_minute: float = 0.0


@dataclass
class TransferStats:
    """Per-series throughput statistics with median aggregation.

    Only series with at least ``MIN_IMAGES_FOR_STATS`` images are
    included in the speed statistics.  Smaller series are still counted
    towards ``total_images`` but their transfer speed is too noisy to
    be meaningful.
    """
    MIN_IMAGES_FOR_STATS: int = 10

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
    # Log
    log_message = Signal(str)
    # Unknown institution detected (institution_name)
    unknown_institution = Signal(str)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class TransferEngine:
    """
    Continuous service: query all sources → build queue → transfer → sleep → repeat.
    """

    def __init__(self, config: Any):
        self.config = config
        self.signals = TransferSignals()
        self.stats = TransferStats()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._queue: List[SeriesJob] = []
        self._notified_institutions: Set[str] = set()

    @property
    def is_running(self) -> bool:
        return self._running

    # -- public API ----------------------------------------------------------

    def start(self, hours: int, max_images: int, sync_interval: int):
        """Start the continuous service loop."""
        if self._running:
            return
        self._cancel.clear()
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

    # -- internal ------------------------------------------------------------

    def _log(self, msg: str):
        logger.info(msg)
        self.signals.log_message.emit(msg)

    def _service_loop(self, hours: int, max_images: int, sync_interval: int):
        self.stats.start_session()
        self._log(f"Service started — downloading last {hours}h, "
                  f"max {max_images or 'unlimited'} img/series, "
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
                    self._log(f"Cycle {cycle} done — {images_this_cycle} images transferred.")
                else:
                    self._log(f"Cycle {cycle} — no new images. "
                              f"Waiting {sync_interval}s...")

                # Sleep in small steps so we can react to cancel quickly
                for _ in range(sync_interval):
                    if self._cancel.is_set():
                        break
                    time.sleep(1)

        except Exception as e:
            self._log(f"Service error: {e}")
            logger.exception("Service loop error")
        finally:
            self._running = False
            self._log("Service stopped.")
            self.signals.service_stopped.emit()

    def _run_one_cycle(self, hours: int, max_images: int) -> int:
        """Query every source PACS, build queue, transfer everything."""
        now = datetime.now()
        cutoff = now - timedelta(hours=hours)
        yesterday = now - timedelta(days=1)
        date_range = f"{yesterday.strftime('%Y%m%d')}-{now.strftime('%Y%m%d')}"

        all_jobs: List[SeriesJob] = []
        seen_series: Set[str] = set()

        for remote_key in self.config.remote_nodes:
            if self._cancel.is_set():
                break
            try:
                jobs = self._query_source(
                    remote_key, date_range, cutoff,
                    max_images, seen_series)
                all_jobs.extend(jobs)
            except Exception as e:
                self._log(f"  Error querying {remote_key}: {e}")

        if not all_jobs:
            self._queue = []
            self.signals.queue_updated.emit([])
            return 0

        self._queue = all_jobs
        self.signals.queue_updated.emit([j.to_dict() for j in all_jobs])
        self._log(f"Queue: {len(all_jobs)} series to download")

        # Transfer all queued series
        total_images = 0
        for job in all_jobs:
            if self._cancel.is_set():
                break
            total_images += self._transfer_series(job)
            self.signals.queue_updated.emit([j.to_dict() for j in all_jobs])

        return total_images

    def _query_source(self, remote_key: str, date_range: str,
                      cutoff: datetime, max_images: int,
                      seen_series: Set[str]) -> List[SeriesJob]:
        """Query one source PACS and return new SeriesJob items."""
        dicom_ops = self._make_dicom_ops(remote_key)
        self._log(f"Querying {remote_key}...")
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

        self._log(f"  {remote_key}: {len(studies)} studies in time window")

        jobs: List[SeriesJob] = []
        for study_ds in studies:
            if self._cancel.is_set():
                break
            jobs.extend(self._build_study_jobs(
                dicom_ops, study_ds, seen_series, max_images))

        # Handle prior studies
        if self.config.prior_studies_count > 0:
            prior_jobs = self._resolve_priors(
                dicom_ops, studies, seen_series, max_images)
            jobs.extend(prior_jobs)

        return jobs

    def _build_study_jobs(
            self, dicom_ops: DicomOperations, study_ds,
            seen_series: Set[str], max_images: int) -> List[SeriesJob]:
        """Build SeriesJob items for one study."""
        study_uid = getattr(study_ds, 'StudyInstanceUID', '')
        patient_name = str(getattr(study_ds, 'PatientName', 'Unknown'))
        patient_id = getattr(study_ds, 'PatientID', '')
        study_desc = getattr(study_ds, 'StudyDescription', 'N/A')
        institution = str(
            getattr(study_ds, 'InstitutionName', '')).strip()

        series_list = dicom_ops.c_find_series(study_uid)

        # InstitutionName fallback: read from first series
        if not institution and series_list:
            institution = str(
                getattr(series_list[0], 'InstitutionName', '')).strip()

        if not self._passes_institution_filter(institution):
            return []

        local_series = self._fetch_local_series_counts(
            dicom_ops, study_uid)

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
            ))
        return jobs

    def _transfer_series(self, job: SeriesJob) -> int:
        """Transfer one series. Returns number of images transferred."""
        job.status = "transferring"
        self.signals.series_started.emit(job.to_dict())

        to_transfer = job.to_transfer
        self._log(f"Downloading: {job.patient_name} — "
                  f"[{job.modality}] {job.series_description} "
                  f"({to_transfer} images)")

        # Try all configured sources until one succeeds
        for remote_key in self.config.remote_nodes:
            if self._cancel.is_set():
                job.status = "error"
                return 0
            try:
                ops = self._make_dicom_ops(remote_key)
                t_start = time.time()
                success, images = ops.c_move_series(job.study_uid, job.series_uid)
                t_elapsed = time.time() - t_start
                if success:
                    images = max(images, to_transfer)
                    self.stats.record_series(
                        job.series_uid, images, t_elapsed)
                    ipm = (images / t_elapsed) * 60 if t_elapsed > 0 else 0.0
                    job.images_per_minute = ipm
                    job.status = "done"
                    self.signals.series_completed.emit(job.series_uid, images)
                    self.signals.stats_updated.emit(self.stats)
                    return images
            except Exception as e:
                self._log(f"  C-MOVE via {remote_key} failed: {e}")

        job.status = "error"
        self.signals.series_error.emit(job.series_uid, "Transfer failed on all sources")
        return 0

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

            current_uids = {getattr(s, 'StudyInstanceUID', '') for s in current_studies
                            if getattr(s, 'PatientID', '') == pid}

            all_raw = dicom_ops.c_find_studies(patient_id=pid)
            prior_studies = []
            for s in all_raw:
                uid = getattr(s, 'StudyInstanceUID', '')
                if uid in current_uids:
                    continue
                prior_studies.append(s)

            prior_studies.sort(
                key=lambda x: (getattr(x, 'StudyDate', ''),
                               getattr(x, 'StudyTime', '')),
                reverse=True)

            # Filter by same modality if configured
            if self.config.prior_studies_same_modality:
                target_mods = set()
                for cs in current_studies:
                    if getattr(cs, 'PatientID', '') == pid:
                        mods = str(getattr(cs, 'ModalitiesInStudy', ''))
                        for m in mods.replace("\\", ",").split(","):
                            m = m.strip()
                            if m:
                                target_mods.add(m)
                if target_mods:
                    prior_studies = [
                        s for s in prior_studies
                        if {m.strip()
                            for m in str(getattr(s, 'ModalitiesInStudy', ''))
                            .replace("\\", ",").split(",")
                            if m.strip()} & target_mods
                    ]

            count = min(self.config.prior_studies_count, len(prior_studies))
            for ps in prior_studies[:count]:
                ps_uid = getattr(ps, 'StudyInstanceUID', '')
                ps_name = str(getattr(ps, 'PatientName', 'Unknown'))
                ps_desc = getattr(ps, 'StudyDescription', 'N/A')

                series_list = dicom_ops.c_find_series(ps_uid)
                local_series = self._fetch_local_series_counts(
                    dicom_ops, ps_uid)

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
                    prior_jobs.append(SeriesJob(
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
                    ))

            if prior_jobs:
                self._log(f"  {len(prior_jobs)} prior series for patient {pid}")

        return prior_jobs

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
        if remote_count > 10 and missing <= 2:
            return True
        return False

    @staticmethod
    def _fetch_local_series_counts(
            dicom_ops: DicomOperations, study_uid: str) -> Dict[str, int]:
        """Query local PACS and return {series_uid: image_count}."""
        counts: Dict[str, int] = {}
        try:
            for ls in dicom_ops.c_find_local_series(study_uid):
                uid = getattr(ls, 'SeriesInstanceUID', '')
                cnt = int(
                    getattr(ls, 'NumberOfSeriesRelatedInstances', 0) or 0)
                if uid:
                    counts[uid] = cnt
        except Exception:
            pass
        return counts

    def _make_dicom_ops(self, remote_key: str) -> DicomOperations:
        remote_node = self.config.remote_nodes[remote_key]
        return DicomOperations(
            self.config.get_local_dict(),
            remote_node.to_dict(),
            remote_key,
        )
