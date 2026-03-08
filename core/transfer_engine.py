"""
Transfer engine for DICOM Sync GUI.
Runs a continuous service loop: query → compare → transfer → wait → repeat.
Emits Qt signals so the GUI can display queue and progress in real time.
"""

import logging
import math
import threading
import time
from collections import deque
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
        }


@dataclass
class TransferStats:
    """Throughput statistics."""
    _timestamps: deque = field(default_factory=lambda: deque(maxlen=10000))
    total_images: int = 0
    start_time: float = 0.0
    _active_periods: list = field(default_factory=list)
    _current_active_start: Optional[float] = None

    def start_session(self):
        self.start_time = time.time()
        self.total_images = 0
        self._timestamps.clear()
        self._active_periods = []
        self._current_active_start = None

    def mark_active(self):
        if self._current_active_start is None:
            self._current_active_start = time.time()

    def mark_idle(self):
        if self._current_active_start is not None:
            self._active_periods.append(
                (self._current_active_start, time.time()))
            self._current_active_start = None

    def record_image(self, count: int = 1):
        now = time.time()
        self._timestamps.append((now, count))
        self.total_images += count

    def _active_seconds_in_window(self, window_seconds: float) -> float:
        now = time.time()
        cutoff = now - window_seconds
        total = 0.0
        for start, end in self._active_periods:
            if end < cutoff:
                continue
            s = max(start, cutoff)
            total += end - s
        if self._current_active_start is not None:
            s = max(self._current_active_start, cutoff)
            total += now - s
        return max(total, 0.001)

    def _images_in_window(self, window_seconds: float) -> int:
        cutoff = time.time() - window_seconds
        return sum(c for t, c in self._timestamps if t >= cutoff)

    def images_per_minute(self, window_minutes: float) -> float:
        window_sec = window_minutes * 60
        images = self._images_in_window(window_sec)
        active_sec = self._active_seconds_in_window(window_sec)
        return (images / active_sec) * 60

    def overall_images_per_minute(self) -> float:
        if self.start_time == 0:
            return 0.0
        total_active = sum(end - start for start, end in self._active_periods)
        if self._current_active_start is not None:
            total_active += time.time() - self._current_active_start
        if total_active < 1:
            return 0.0
        return (self.total_images / total_active) * 60

    def overall_mean_and_std(self) -> Tuple[float, float]:
        if self.start_time == 0 or self.total_images == 0:
            return 0.0, 0.0
        if not self._timestamps:
            return 0.0, 0.0
        buckets = []
        now = time.time()
        bucket_size = 10
        earliest = self._timestamps[0][0] if self._timestamps else now
        t = earliest
        while t < now:
            bucket_end = t + bucket_size
            count = sum(c for ts, c in self._timestamps if t <= ts < bucket_end)
            active_in_bucket = False
            for start, end in self._active_periods:
                if start < bucket_end and end > t:
                    active_in_bucket = True
                    break
            if (self._current_active_start is not None
                    and self._current_active_start < bucket_end):
                active_in_bucket = True
            if active_in_bucket and count > 0:
                buckets.append(count * (60.0 / bucket_size))
            t = bucket_end
        if not buckets:
            return self.overall_images_per_minute(), 0.0
        mean = sum(buckets) / len(buckets)
        if len(buckets) < 2:
            return mean, 0.0
        variance = sum((x - mean) ** 2 for x in buckets) / (len(buckets) - 1)
        std = math.sqrt(variance)
        return mean, std


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

        for remote_key, remote_node in self.config.remote_nodes.items():
            if self._cancel.is_set():
                break
            try:
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

                for study_ds in studies:
                    if self._cancel.is_set():
                        break
                    study_uid = getattr(study_ds, 'StudyInstanceUID', '')
                    patient_name = str(getattr(study_ds, 'PatientName', 'Unknown'))
                    patient_id = getattr(study_ds, 'PatientID', '')
                    study_desc = getattr(study_ds, 'StudyDescription', 'N/A')

                    series_list = dicom_ops.c_find_series(study_uid)
                    local_series = {}
                    try:
                        for ls in dicom_ops.c_find_local_series(study_uid):
                            uid = getattr(ls, 'SeriesInstanceUID', '')
                            cnt = int(getattr(ls, 'NumberOfSeriesRelatedInstances', 0) or 0)
                            if uid:
                                local_series[uid] = cnt
                    except Exception:
                        pass

                    for ser in series_list:
                        series_uid = getattr(ser, 'SeriesInstanceUID', '')
                        if series_uid in seen_series:
                            continue
                        remote_count = int(
                            getattr(ser, 'NumberOfSeriesRelatedInstances', 0) or 0)
                        local_count = local_series.get(series_uid, 0)

                        if remote_count == 0 or local_count >= remote_count:
                            continue
                        if max_images > 0 and remote_count > max_images:
                            continue
                        missing = remote_count - local_count
                        if remote_count > 10 and missing <= 2:
                            continue

                        seen_series.add(series_uid)
                        job = SeriesJob(
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
                        )
                        all_jobs.append(job)

                # Handle prior studies
                if self.config.prior_studies_count > 0:
                    prior_jobs = self._resolve_priors(
                        dicom_ops, studies, seen_series, max_images)
                    all_jobs.extend(prior_jobs)

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
            # Update queue after each series
            self.signals.queue_updated.emit([j.to_dict() for j in all_jobs])

        return total_images

    def _transfer_series(self, job: SeriesJob) -> int:
        """Transfer one series. Returns number of images transferred."""
        job.status = "transferring"
        self.signals.series_started.emit(job.to_dict())

        to_transfer = job.to_transfer
        self._log(f"Downloading: {job.patient_name} — "
                  f"[{job.modality}] {job.series_description} "
                  f"({to_transfer} images)")

        # Find which remote has this series
        dicom_ops = None
        for remote_key in self.config.remote_nodes:
            dicom_ops = self._make_dicom_ops(remote_key)
            break  # We'll try the first remote that works
        # Actually, we need to try the right source. For now, try all.
        for remote_key in self.config.remote_nodes:
            if self._cancel.is_set():
                job.status = "error"
                return 0
            try:
                ops = self._make_dicom_ops(remote_key)
                self.stats.mark_active()
                success, images = ops.c_move_series(job.study_uid, job.series_uid)
                self.stats.mark_idle()
                if success:
                    images = max(images, to_transfer)
                    self.stats.record_image(images)
                    job.status = "done"
                    self.signals.series_completed.emit(job.series_uid, images)
                    self.signals.stats_updated.emit(self.stats)
                    return images
            except Exception as e:
                self.stats.mark_idle()
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
                local_series = {}
                try:
                    for ls in dicom_ops.c_find_local_series(ps_uid):
                        uid = getattr(ls, 'SeriesInstanceUID', '')
                        cnt = int(getattr(ls, 'NumberOfSeriesRelatedInstances', 0) or 0)
                        if uid:
                            local_series[uid] = cnt
                except Exception:
                    pass

                for ser in series_list:
                    series_uid = getattr(ser, 'SeriesInstanceUID', '')
                    if series_uid in seen_series:
                        continue
                    remote_count = int(
                        getattr(ser, 'NumberOfSeriesRelatedInstances', 0) or 0)
                    local_count = local_series.get(series_uid, 0)
                    if remote_count == 0 or local_count >= remote_count:
                        continue
                    if max_images > 0 and remote_count > max_images:
                        continue
                    missing = remote_count - local_count
                    if remote_count > 10 and missing <= 2:
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

    def _make_dicom_ops(self, remote_key: str) -> DicomOperations:
        remote_node = self.config.remote_nodes[remote_key]
        return DicomOperations(
            self.config.get_local_dict(),
            remote_node.to_dict(),
            remote_key,
        )
