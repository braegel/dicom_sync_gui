"""
SQLite-based transfer performance log for regulatory compliance.

Records per-series and per-study transfer metrics. Patient-identifiable
fields (PatientID, AccessionNumber, UIDs) are stored as SHA-256 hashes.
"""

import hashlib
import os
import platform
import sqlite3
import threading
from datetime import datetime
from typing import Dict, List, Optional

# Approximate uncompressed bytes per image, by modality.
MODALITY_BYTES_PER_IMAGE: Dict[str, int] = {
    "CT": 512 * 512 * 2,       # 524 288
    "MR": 256 * 256 * 2,       # 131 072
    "CR": 2048 * 2048 * 2,     # 8 388 608
    "DX": 3000 * 3000 * 2,     # 18 000 000
    "US": 640 * 480 * 3,       # 921 600
    "PT": 128 * 128 * 2,       # 32 768
    "NM": 256 * 256 * 2,       # 131 072
    "MG": 4000 * 3000 * 2,     # 24 000 000
    "XA": 512 * 512 * 2,       # 524 288
}
_DEFAULT_BYTES_PER_IMAGE = 512 * 512 * 2


def estimate_bytes(modality: str, image_count: int) -> int:
    return image_count * MODALITY_BYTES_PER_IMAGE.get(modality, _DEFAULT_BYTES_PER_IMAGE)


def default_db_path() -> str:
    system = platform.system()
    if system == "Darwin":
        log_dir = os.path.expanduser("~/Library/Logs")
    elif system == "Windows":
        log_dir = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        log_dir = os.environ.get(
            "XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return os.path.join(log_dir, "transfer_log.sqlite")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_SERIES_TABLE = """
CREATE TABLE IF NOT EXISTS series_transfer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source_pacs TEXT NOT NULL,
    study_uid_hash TEXT NOT NULL,
    series_uid_hash TEXT NOT NULL,
    patient_id_hash TEXT NOT NULL,
    accession_number_hash TEXT NOT NULL,
    study_date TEXT NOT NULL,
    study_time TEXT NOT NULL,
    modality TEXT NOT NULL,
    study_description TEXT NOT NULL,
    series_description TEXT NOT NULL,
    series_number TEXT NOT NULL,
    image_count INTEGER NOT NULL,
    duration_seconds REAL NOT NULL,
    images_per_minute REAL NOT NULL,
    estimated_bytes INTEGER NOT NULL,
    estimated_mbps REAL NOT NULL
)
"""

_FAILURES_TABLE = """
CREATE TABLE IF NOT EXISTS series_failures (
    source_pacs TEXT NOT NULL,
    series_uid_hash TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT NOT NULL,
    PRIMARY KEY (source_pacs, series_uid_hash)
)
"""


_STUDY_TABLE = """
CREATE TABLE IF NOT EXISTS study_transfer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source_pacs TEXT NOT NULL,
    study_uid_hash TEXT NOT NULL,
    patient_id_hash TEXT NOT NULL,
    accession_number_hash TEXT NOT NULL,
    study_date TEXT NOT NULL,
    study_time TEXT NOT NULL,
    modality TEXT NOT NULL,
    study_description TEXT NOT NULL,
    total_series INTEGER NOT NULL,
    total_images INTEGER NOT NULL,
    total_duration_seconds REAL NOT NULL,
    wall_clock_seconds REAL NOT NULL,
    total_estimated_bytes INTEGER NOT NULL,
    estimated_mbps REAL NOT NULL
)
"""


class TransferLog:

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SERIES_TABLE)
        self._conn.execute(_STUDY_TABLE)
        self._conn.execute(_FAILURES_TABLE)
        self._conn.commit()

    def close(self):
        self._conn.close()

    def record_series(self, *, source_pacs: str, study_uid: str,
                      series_uid: str, patient_id: str,
                      accession_number: str, study_date: str,
                      study_time: str, modality: str,
                      study_description: str, series_description: str,
                      series_number: str, image_count: int,
                      duration_seconds: float,
                      timestamp: Optional[str] = None):
        ipm = (image_count / duration_seconds) * 60 if duration_seconds > 0 else 0.0
        est_bytes = estimate_bytes(modality, image_count)
        est_mbps = (est_bytes * 8) / (duration_seconds * 1_000_000) if duration_seconds > 0 else 0.0
        ts = timestamp if timestamp is not None else datetime.now().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO series_transfer "
                "(timestamp, source_pacs, study_uid_hash, series_uid_hash, "
                "patient_id_hash, accession_number_hash, study_date, study_time, "
                "modality, study_description, series_description, series_number, "
                "image_count, duration_seconds, images_per_minute, "
                "estimated_bytes, estimated_mbps) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts,
                 source_pacs,
                 _sha256(study_uid),
                 _sha256(series_uid),
                 _sha256(patient_id),
                 _sha256(accession_number),
                 study_date, study_time,
                 modality, study_description, series_description,
                 series_number, image_count, duration_seconds,
                 ipm, est_bytes, est_mbps))
            self._conn.commit()

    def record_study(self, *, source_pacs: str, study_uid: str,
                     patient_id: str, accession_number: str,
                     study_date: str, study_time: str, modality: str,
                     study_description: str, total_series: int,
                     total_images: int, total_duration_seconds: float,
                     wall_clock_seconds: float):
        est_bytes = estimate_bytes(modality, total_images)
        est_mbps = (est_bytes * 8) / (wall_clock_seconds * 1_000_000) if wall_clock_seconds > 0 else 0.0
        with self._lock:
            self._conn.execute(
                "INSERT INTO study_transfer "
                "(timestamp, source_pacs, study_uid_hash, patient_id_hash, "
                "accession_number_hash, study_date, study_time, modality, "
                "study_description, total_series, total_images, "
                "total_duration_seconds, wall_clock_seconds, "
                "total_estimated_bytes, estimated_mbps) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(),
                 source_pacs,
                 _sha256(study_uid),
                 _sha256(patient_id),
                 _sha256(accession_number),
                 study_date, study_time, modality, study_description,
                 total_series, total_images,
                 total_duration_seconds, wall_clock_seconds,
                 est_bytes, est_mbps))
            self._conn.commit()

    def record_series_failure(self, *, source_pacs: str,
                              series_uid: str):
        h = _sha256(series_uid)
        ts = datetime.now().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO series_failures "
                "(source_pacs, series_uid_hash, attempt_count, "
                "last_attempt_at) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(source_pacs, series_uid_hash) DO UPDATE SET "
                "attempt_count = attempt_count + 1, "
                "last_attempt_at = excluded.last_attempt_at",
                (source_pacs, h, ts))
            self._conn.commit()

    def get_series_failure_count(self, *, source_pacs: str,
                                 series_uid: str) -> int:
        h = _sha256(series_uid)
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt_count FROM series_failures "
                "WHERE source_pacs = ? AND series_uid_hash = ?",
                (source_pacs, h)).fetchone()
        return int(row[0]) if row else 0

    def is_series_blacklisted(self, *, source_pacs: str,
                              series_uid: str,
                              max_attempts: int = 2) -> bool:
        return self.get_series_failure_count(
            source_pacs=source_pacs,
            series_uid=series_uid) >= max_attempts

    def clear_series_failures(self, *, source_pacs: str,
                              series_uid: str):
        h = _sha256(series_uid)
        with self._lock:
            self._conn.execute(
                "DELETE FROM series_failures "
                "WHERE source_pacs = ? AND series_uid_hash = ?",
                (source_pacs, h))
            self._conn.commit()

    def query_series(self, *, date_from: Optional[str] = None,
                     date_to: Optional[str] = None,
                     source_pacs: Optional[str] = None,
                     modality: Optional[str] = None,
                     patient_id: Optional[str] = None,
                     accession_number: Optional[str] = None) -> List[dict]:
        return self._query("series_transfer", date_from=date_from,
                           date_to=date_to, source_pacs=source_pacs,
                           modality=modality, patient_id=patient_id,
                           accession_number=accession_number)

    def query_studies(self, *, date_from: Optional[str] = None,
                      date_to: Optional[str] = None,
                      source_pacs: Optional[str] = None,
                      modality: Optional[str] = None,
                      patient_id: Optional[str] = None,
                      accession_number: Optional[str] = None) -> List[dict]:
        return self._query("study_transfer", date_from=date_from,
                           date_to=date_to, source_pacs=source_pacs,
                           modality=modality, patient_id=patient_id,
                           accession_number=accession_number)

    def _query(self, table: str, *, date_from, date_to, source_pacs,
               modality, patient_id, accession_number) -> List[dict]:
        clauses: List[str] = []
        params: list = []
        if date_from:
            clauses.append("study_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("study_date <= ?")
            params.append(date_to)
        if source_pacs:
            clauses.append("source_pacs = ?")
            params.append(source_pacs)
        if modality:
            clauses.append("modality = ?")
            params.append(modality)
        if patient_id:
            clauses.append("patient_id_hash = ?")
            params.append(_sha256(patient_id))
        if accession_number:
            clauses.append("accession_number_hash = ?")
            params.append(_sha256(accession_number))
        sql = f"SELECT * FROM {table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
