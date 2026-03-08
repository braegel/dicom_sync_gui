"""
Transfer engine for DICOM Sync GUI.
Runs transfers in background threads and emits Qt signals for dashboard updates.
"""

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QObject, Signal

from core.dicom_ops import DicomOperations, parse_dicom_date, parse_dicom_time

logger = logging.getLogger("dicom_sync")


# --- Data classes ---

@dataclass
class SeriesTransferInfo:
    """Tracks a single series transfer."""
    patient_name: str = ""
    patient_id: str = ""
    study_description: str = ""
    series_description: str = ""
    modality: str = ""
    series_number: str = ""
    study_date: str = ""
    study_time: str = ""
    institution: str = ""
    study_uid: str = ""
    series_uid: str = ""
    remote_count: int = 0
    local_count: int = 0
    images_transferred: int = 0
    status: str = "pending"  # pending, transferring, complete, error


@dataclass
class TransferStats:
    """Transfer throughput statistics."""
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


# --- Signals ---

class TransferSignals(QObject):
    """Qt signals emitted during transfers."""
    series_started = Signal(dict)
    series_progress = Signal(str, int, int)  # series_uid, transferred, total
    series_completed = Signal(str, int)      # series_uid, total_images
    series_error = Signal(str, str)          # series_uid, error_message
    stats_updated = Signal(object)           # TransferStats
    image_received = Signal()
    transfer_job_started = Signal(int)       # total series count
    transfer_job_finished = Signal(int)      # total images transferred
    log_message = Signal(str)


# --- Transfer Engine ---

class TransferEngine:
    """Manages DICOM transfers with GUI feedback."""

    def __init__(self, dicom_ops: DicomOperations, config: Any):
        self.dicom_ops = dicom_ops
        self.config = config
        self.signals = TransferSignals()
        self.stats = TransferStats()
        self._cancel_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def cancel(self):
        self._cancel_flag.set()

    def transfer_studies(self, studies: List[Dict],
                         prior_studies: Optional[List[Dict]] = None):
        if self._running:
            return
        self._cancel_flag.clear()
        self._running = True

        all_studies = list(studies)
        if prior_studies:
            all_studies.extend(prior_studies)

        self._thread = threading.Thread(
            target=self._run_transfer, args=(all_studies,), daemon=True)
        self._thread.start()

    def _run_transfer(self, studies: List[Dict]):
        try:
            self.stats.start_session()
            all_series = []

            for study in studies:
                study_uid = study["study_uid"]
                series_list = self.dicom_ops.c_find_series(study_uid)

                local_series = {}
                try:
                    for ls in self.dicom_ops.c_find_local_series(study_uid):
                        uid = getattr(ls, 'SeriesInstanceUID', '')
                        count = int(getattr(
                            ls, 'NumberOfSeriesRelatedInstances', 0) or 0)
                        if uid:
                            local_series[uid] = count
                except Exception:
                    pass

                for series in series_list:
                    series_uid = getattr(series, 'SeriesInstanceUID', '')
                    remote_count = int(getattr(
                        series, 'NumberOfSeriesRelatedInstances', 0) or 0)
                    local_count = local_series.get(series_uid, 0)

                    if remote_count == 0 or local_count >= remote_count:
                        continue
                    if (self.config.max_images > 0
                            and remote_count > self.config.max_images):
                        continue
                    missing = remote_count - local_count
                    if remote_count > 10 and missing <= 2:
                        continue

                    info = SeriesTransferInfo(
                        patient_name=study.get("patient_name", ""),
                        patient_id=study.get("patient_id", ""),
                        study_description=study.get("study_description", ""),
                        series_description=getattr(
                            series, 'SeriesDescription', 'N/A'),
                        modality=getattr(series, 'Modality', 'UN'),
                        series_number=str(
                            getattr(series, 'SeriesNumber', '')),
                        study_date=study.get("study_date", ""),
                        study_time=study.get("study_time", ""),
                        institution=study.get("institution", ""),
                        study_uid=study_uid,
                        series_uid=series_uid,
                        remote_count=remote_count,
                        local_count=local_count,
                    )
                    all_series.append(info)

            if not all_series:
                self.signals.log_message.emit("No series to download.")
                self.signals.transfer_job_finished.emit(0)
                return

            self.signals.transfer_job_started.emit(len(all_series))
            total_transferred = 0

            for info in all_series:
                if self._cancel_flag.is_set():
                    self.signals.log_message.emit("Transfer cancelled.")
                    break

                self.signals.series_started.emit({
                    "patient_name": info.patient_name,
                    "patient_id": info.patient_id,
                    "study_description": info.study_description,
                    "series_description": info.series_description,
                    "modality": info.modality,
                    "series_number": info.series_number,
                    "study_date": info.study_date,
                    "series_uid": info.series_uid,
                    "remote_count": info.remote_count,
                    "local_count": info.local_count,
                })

                to_transfer = info.remote_count - info.local_count
                self.signals.log_message.emit(
                    f"Downloading: {info.patient_name} - "
                    f"{info.study_description} - "
                    f"[{info.modality}] {info.series_description} "
                    f"({to_transfer} images)")

                self.stats.mark_active()

                success, images = self.dicom_ops.c_move_series(
                    info.study_uid, info.series_uid)
                if success:
                    images = to_transfer
                else:
                    images = 0

                self.stats.mark_idle()

                if images > 0:
                    self.stats.record_image(images)
                    total_transferred += images
                    self.signals.series_completed.emit(
                        info.series_uid, images)
                    self.signals.stats_updated.emit(self.stats)
                else:
                    self.signals.series_error.emit(
                        info.series_uid, "Transfer failed")

            self.signals.transfer_job_finished.emit(total_transferred)

        except Exception as e:
            logger.error(f"Transfer error: {e}")
            self.signals.log_message.emit(f"Error: {e}")
            self.signals.transfer_job_finished.emit(0)
        finally:
            self._running = False
