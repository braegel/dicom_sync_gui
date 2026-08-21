"""
SQLite-based transfer performance log for regulatory compliance.

Records per-series and per-study transfer metrics. Patient-identifiable
fields (PatientID, AccessionNumber, UIDs) are stored as SHA-256 hashes.
"""

import hashlib
import logging
import os
import platform
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("dicom_sync")

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


# Indexes for the columns ``_query`` filters on selectively, and for
# the column ``mbps_stats`` sorts by.  Without them every lookup is a
# full scan over the whole history — append-only, tens of thousands of
# rows within months of 24/7 operation.
#
# Measured on a real 58 757-row log (times per query, mean of 15):
#
#   patient_id_hash = ?          8.7 ms  ->  0.0 ms
#   study_date over one day      5.6 ms  ->  0.0 ms
#   ORDER BY estimated_mbps     28.2 ms  ->  9.7 ms
#   source_pacs = ?            112.9 ms  -> 114.5 ms   (no gain)
#
# So ``source_pacs`` and ``modality`` are deliberately NOT indexed:
# both have a handful of distinct values, an index over them matches
# most of the table, and SQLite then does a rowid lookup per row —
# slower than the sequential scan it replaces, plus write cost on the
# engine's hot path.  Selectivity is the whole criterion here; do not
# add an index without measuring it the same way.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_series_study_date "
    "ON series_transfer (study_date)",
    "CREATE INDEX IF NOT EXISTS idx_series_patient "
    "ON series_transfer (patient_id_hash)",
    "CREATE INDEX IF NOT EXISTS idx_series_accession "
    "ON series_transfer (accession_number_hash)",
    "CREATE INDEX IF NOT EXISTS idx_series_mbps "
    "ON series_transfer (estimated_mbps)",
    "CREATE INDEX IF NOT EXISTS idx_study_study_date "
    "ON study_transfer (study_date)",
    "CREATE INDEX IF NOT EXISTS idx_study_patient "
    "ON study_transfer (patient_id_hash)",
)


# Per-table schema for ``_insert``: maps column name to the Python
# type allowed for its value.  Keeping the schema authoritative here
# means a stray caller passing attacker-controlled column names
# cannot smuggle SQL into the generated INSERT — and a value-type
# mistake (e.g. ``image_count="oops"``) gets caught up-front instead
# of as a SQLite "Datatype mismatch" deep in the round-trip.
_INSERT_SCHEMA = {
    "series_transfer": {
        "timestamp": str,
        "source_pacs": str,
        "study_uid_hash": str,
        "series_uid_hash": str,
        "patient_id_hash": str,
        "accession_number_hash": str,
        "study_date": str,
        "study_time": str,
        "modality": str,
        "study_description": str,
        "series_description": str,
        "series_number": str,
        "image_count": int,
        "duration_seconds": (int, float),
        "images_per_minute": (int, float),
        "estimated_bytes": int,
        "estimated_mbps": (int, float),
    },
    "study_transfer": {
        "timestamp": str,
        "source_pacs": str,
        "study_uid_hash": str,
        "patient_id_hash": str,
        "accession_number_hash": str,
        "study_date": str,
        "study_time": str,
        "modality": str,
        "study_description": str,
        "total_series": int,
        "total_images": int,
        "total_duration_seconds": (int, float),
        "wall_clock_seconds": (int, float),
        "total_estimated_bytes": int,
        "estimated_mbps": (int, float),
    },
}
_INSERT_COLUMNS = {table: frozenset(spec)
                   for table, spec in _INSERT_SCHEMA.items()}


class TransferLog:
    """SQLite-backed transfer performance log.

    The database is opened in **WAL** journal mode so a long-running
    read query in one ``TransferLog`` instance (e.g. the Transfer
    Performance Statistics window scanning the full history) cannot
    block the engine's writes from another instance.  Multiple
    ``TransferLog`` instances on the same file are safe to use
    concurrently across threads.

    Within a single instance the in-process ``_lock`` still
    serializes accesses to its own SQLite connection — SQLite's
    Python binding requires that even with WAL.
    """

    def __init__(self, db_path: str):
        parent_dir = os.path.dirname(db_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Enable WAL so readers never block writers (and vice versa).
        # WAL is a per-database setting persisted in the file header,
        # so every future TransferLog opening this file inherits it.
        # Only flip the mode if the file isn't already in WAL — the
        # extra PRAGMA round-trip is cheap but pointless on every
        # re-open of an existing log.
        try:
            current = self._conn.execute(
                "PRAGMA journal_mode").fetchone()
            mode = (current[0] if current else "").lower()
            if mode != "wal":
                self._conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL is the WAL-recommended sync mode: durable across
            # process crashes, only a power loss could lose the most
            # recent commit — acceptable for a non-clinical-record log.
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as e:
            logger.warning(
                f"TransferLog: could not enable WAL journal mode "
                f"({e}); falling back to default rollback journal")
        self._conn.execute(_SERIES_TABLE)
        self._conn.execute(_STUDY_TABLE)
        self._conn.execute(_FAILURES_TABLE)
        # IF NOT EXISTS, so an existing log picks these up on the next
        # open without a migration step.
        for stmt in _INDEXES:
            self._conn.execute(stmt)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TransferLog":
        """Support ``with TransferLog(path) as log:``.

        Every short-lived caller was already writing the ``try/finally
        … close()`` by hand; forgetting it leaks a SQLite connection
        and, with WAL, leaves the -wal sidecar behind for the next
        opener to replay.
        """
        return self

    def __exit__(self, exc_type: object, exc: object,
                 tb: object) -> None:
        self.close()

    def _insert(self, table: str, fields: Dict[str, Any]) -> None:
        """Insert a row built from a ``{column: value}`` mapping.

        Centralizes the parameterized-INSERT boilerplate so callers
        only describe the column → value mapping and don't have to
        keep the column list and the value list in lockstep.

        The *table* must be one of the keys in ``_INSERT_COLUMNS``
        and every column in *fields* must be in that whitelist.
        Values go through SQLite parameters, but column / table
        names are string-interpolated — the whitelist is what makes
        that safe.
        """
        schema = _INSERT_SCHEMA.get(table)
        if schema is None:
            raise ValueError(
                f"TransferLog._insert: unknown table {table!r}")
        unknown = set(fields) - set(schema)
        if unknown:
            raise ValueError(
                f"TransferLog._insert: columns "
                f"{sorted(unknown)} not allowed for table {table!r}")
        for col, value in fields.items():
            expected = schema[col]
            # ``bool`` is a subclass of ``int`` in Python — without an
            # explicit guard, ``image_count=True`` would silently land
            # as ``1``.  Reject bool unless the column actually allows
            # it (none currently do).
            expected_types = expected if isinstance(expected, tuple) else (expected,)
            if isinstance(value, bool) and bool not in expected_types:
                raise TypeError(
                    f"TransferLog._insert: column {col!r} expects "
                    f"{expected}, got bool ({value!r})")
            if not isinstance(value, expected):
                raise TypeError(
                    f"TransferLog._insert: column {col!r} expects "
                    f"{expected}, got {type(value).__name__} ({value!r})")
        columns = list(fields.keys())
        placeholders = ",".join("?" * len(columns))
        sql = (f"INSERT INTO {table} ({','.join(columns)}) "
               f"VALUES ({placeholders})")
        with self._lock:
            try:
                self._conn.execute(
                    sql, tuple(fields[c] for c in columns))
                self._conn.commit()
            except sqlite3.Error:
                # Roll back so the connection isn't left in a
                # half-committed state for the next caller.
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                logger.error(
                    f"TransferLog._insert into {table!r} failed",
                    exc_info=True)
                raise

    def record_series(self, *, source_pacs: str, study_uid: str,
                      series_uid: str, patient_id: str,
                      accession_number: str, study_date: str,
                      study_time: str, modality: str,
                      study_description: str, series_description: str,
                      series_number: str, image_count: int,
                      duration_seconds: float,
                      timestamp: Optional[str] = None) -> None:
        ipm = (image_count / duration_seconds) * 60 if duration_seconds > 0 else 0.0
        est_bytes = estimate_bytes(modality, image_count)
        est_mbps = (est_bytes * 8) / (duration_seconds * 1_000_000) if duration_seconds > 0 else 0.0
        ts = timestamp if timestamp is not None else datetime.now().isoformat()
        self._insert("series_transfer", {
            "timestamp": ts,
            "source_pacs": source_pacs,
            "study_uid_hash": _sha256(study_uid),
            "series_uid_hash": _sha256(series_uid),
            "patient_id_hash": _sha256(patient_id),
            "accession_number_hash": _sha256(accession_number),
            "study_date": study_date,
            "study_time": study_time,
            "modality": modality,
            "study_description": study_description,
            "series_description": series_description,
            "series_number": series_number,
            "image_count": image_count,
            "duration_seconds": duration_seconds,
            "images_per_minute": ipm,
            "estimated_bytes": est_bytes,
            "estimated_mbps": est_mbps,
        })

    def record_study(self, *, source_pacs: str, study_uid: str,
                     patient_id: str, accession_number: str,
                     study_date: str, study_time: str, modality: str,
                     study_description: str, total_series: int,
                     total_images: int, total_duration_seconds: float,
                     wall_clock_seconds: float) -> None:
        est_bytes = estimate_bytes(modality, total_images)
        est_mbps = (est_bytes * 8) / (wall_clock_seconds * 1_000_000) if wall_clock_seconds > 0 else 0.0
        self._insert("study_transfer", {
            "timestamp": datetime.now().isoformat(),
            "source_pacs": source_pacs,
            "study_uid_hash": _sha256(study_uid),
            "patient_id_hash": _sha256(patient_id),
            "accession_number_hash": _sha256(accession_number),
            "study_date": study_date,
            "study_time": study_time,
            "modality": modality,
            "study_description": study_description,
            "total_series": total_series,
            "total_images": total_images,
            "total_duration_seconds": total_duration_seconds,
            "wall_clock_seconds": wall_clock_seconds,
            "total_estimated_bytes": est_bytes,
            "estimated_mbps": est_mbps,
        })

    def record_series_failure(self, *, source_pacs: str,
                              series_uid: str) -> None:
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
                              max_attempts: int = 3) -> bool:
        return self.get_series_failure_count(
            source_pacs=source_pacs,
            series_uid=series_uid) >= max_attempts

    def clear_series_failures(self, *, source_pacs: str,
                              series_uid: str) -> None:
        h = _sha256(series_uid)
        with self._lock:
            self._conn.execute(
                "DELETE FROM series_failures "
                "WHERE source_pacs = ? AND series_uid_hash = ?",
                (source_pacs, h))
            self._conn.commit()

    # ── Public read API ───────────────────────────────────────────────
    # ``query_series`` and ``query_studies`` are intentional thin
    # passthroughs to ``_query`` — they exist as named entrypoints so
    # callers self-document which row-shape they expect (a series row
    # has a ``series_uid_hash``, a study row doesn't).  Don't fold
    # them into a single ``query(table=…)`` — the explicit names are
    # the value.

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

    def mbps_stats(self) -> Optional[Dict[str, float]]:
        """Return ``{count, mean, stddev, median}`` over all series rows
        with ``estimated_mbps > 0``, or ``None`` if there are < 2 rows.

        Computes mean/stddev in SQL so the dialog doesn't drag the full
        series table into Python on every search.  Holds the connection
        lock only for the actual ``execute`` calls — between the AVG
        query and the median stream the lock is released so the
        engine's writers don't pile up behind a slow median scan.
        """
        # Phase 1: count + mean — short, fixed-cost query.
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, "
                "AVG(estimated_mbps) AS avg "
                "FROM series_transfer "
                "WHERE estimated_mbps > 0").fetchone()
        if not row or row["n"] is None or row["n"] < 2:
            return None
        n = int(row["n"])
        mean = float(row["avg"])

        # Phase 2: variance as the mean of squared deviations, in a
        # second pass with the mean from phase 1.  The one-pass form
        # E[X²] − E[X]² is algebraically identical but catastrophically
        # cancels when the spread is small relative to the mean (think
        # 1000.0 ± 0.5 Mbps on a fast link): both terms are ~1e6 and the
        # difference is ~0.25, so most of the mantissa is lost and the
        # result can even come out negative.  The deviations here are
        # already O(spread), so nothing large is subtracted.
        # Population variance (÷ n, i.e. statistics.pstdev) to match
        # core/stats_utils.py, which the rest of the project uses.
        with self._lock:
            var_row = self._conn.execute(
                "SELECT AVG((estimated_mbps - :mean) "
                "         * (estimated_mbps - :mean)) AS var "
                "FROM series_transfer "
                "WHERE estimated_mbps > 0", {"mean": mean}).fetchone()
        # Still clamped: a sum of squares can only go negative through
        # float noise, but AVG over the same rows is not guaranteed to
        # be bit-identical if a writer slipped in between the passes.
        if not var_row or var_row["var"] is None:
            var = 0.0
        else:
            var = max(float(var_row["var"]), 0.0)
        stddev = var ** 0.5

        # Phase 3: median stream.  Re-acquire the lock for the cursor
        # but release as soon as we've consumed the row(s) we need.
        # For even n the median is the AVERAGE of the two middle rows
        # (indices mid-1 and mid) — the statistics.median convention
        # used everywhere else in the project (review item #26; the
        # old code took only the upper middle row).
        mid = n // 2
        first_idx = mid if n % 2 else mid - 1
        middle: List[float] = []
        with self._lock:
            cur = self._conn.execute(
                "SELECT estimated_mbps FROM series_transfer "
                "WHERE estimated_mbps > 0 ORDER BY estimated_mbps")
            for i, r in enumerate(cur):
                if i >= first_idx:
                    middle.append(float(r[0]))
                if i >= mid:
                    break
        median = sum(middle) / len(middle) if middle else 0.0
        return {"count": n, "mean": mean, "stddev": stddev,
                "median": median}

    def _query(self, table: str, *,
               date_from: Optional[str] = None,
               date_to: Optional[str] = None,
               source_pacs: Optional[str] = None,
               modality: Optional[str] = None,
               patient_id: Optional[str] = None,
               accession_number: Optional[str] = None) -> List[dict]:
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
