"""
Examination lookup dialog — find transfer details by PatientID/AccessionNumber.

The user enters cleartext identifiers which are hashed to query the
SQLite transfer log. Warns if series were likely resent (Mbit/s outlier).
"""

import logging
from datetime import datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QWidget,
)

from core.transfer_log import TransferLog
from gui.async_helpers import run_in_background

logger = logging.getLogger("dicom_sync")

# A series is flagged as a likely resend when its effective throughput
# falls below ``median - RESEND_STDDEV_MULTIPLIER * stddev`` of all
# series in the log.
RESEND_STDDEV_MULTIPLIER = 2


class ExaminationLookupDialog(QDialog):
    """Dialog to look up transfer-log details by PatientID/AccessionNumber."""

    def __init__(self, db_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self.setWindowTitle("Examination Lookup")
        self.setMinimumSize(700, 400)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.input_patient_id = QLineEdit()
        self.input_patient_id.setPlaceholderText("PatientID (cleartext)")
        form.addRow("Patient ID:", self.input_patient_id)
        self.input_accession = QLineEdit()
        self.input_accession.setPlaceholderText("AccessionNumber (cleartext)")
        form.addRow("Accession:", self.input_accession)
        self.input_study_date = QLineEdit()
        self.input_study_date.setPlaceholderText("YYYYMMDD")
        form.addRow("Study Date:", self.input_study_date)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        self.btn_search = QPushButton("Search")
        self.btn_search.clicked.connect(self._search)
        btn_row.addWidget(self.btn_search)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.lbl_resend_warning = QLabel("")
        self.lbl_resend_warning.setWordWrap(True)
        self.lbl_resend_warning.setStyleSheet("color: #e74c3c; font-weight: bold;")
        layout.addWidget(self.lbl_resend_warning)

        self.results_table = QTableWidget()
        self.results_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.results_table.setAlternatingRowColors(True)
        layout.addWidget(self.results_table, 1)

    def _search(self) -> None:
        self.lbl_resend_warning.setText("")
        pid = self.input_patient_id.text().strip()
        acc = self.input_accession.text().strip()
        date = self.input_study_date.text().strip()

        kw = {}
        if pid:
            kw["patient_id"] = pid
        if acc:
            kw["accession_number"] = acc
        if date:
            kw["date_from"] = date
            kw["date_to"] = date

        if not kw:
            self._populate_table([])
            return

        # Query on a worker thread — the log can hold months of
        # history and used to block the GUI thread for the duration
        # of the scan.  The button is disabled until the result
        # lands so a double-click can't start overlapping searches.
        self.btn_search.setEnabled(False)

        def job():
            log = TransferLog(self._db_path)
            try:
                results = log.query_series(**kw)
                # Compute baseline mbps stats in SQL (one round trip)
                # instead of dragging the full series table into Python.
                baseline = log.mbps_stats() if results else None
                return results, baseline
            except Exception:
                # Tagged empty result instead of raising: on_done is
                # only called on success, and a dropped callback would
                # leave the Search button disabled forever.
                logger.exception("Examination lookup query failed")
                return [], None
            finally:
                log.close()

        run_in_background(self, job, self._on_search_results,
                          label="exam_lookup")

    def _on_search_results(self, payload: tuple) -> None:
        results, baseline = payload
        self.btn_search.setEnabled(True)
        self._populate_table(results)
        self._check_resend(results, baseline)

    def _populate_table(self, results: list[dict]) -> None:
        headers = ["Acquisition Time", "Download Start", "Download End",
                    "Source", "Modality", "Study",
                    "Series", "Images", "Duration (s)", "Mbit/s"]
        self.results_table.setColumnCount(len(headers))
        self.results_table.setHorizontalHeaderLabels(headers)
        self.results_table.setRowCount(len(results))
        for row, r in enumerate(results):
            for col, text in enumerate(self._build_row_cells(r)):
                self.results_table.setItem(row, col, QTableWidgetItem(text))

    def _build_row_cells(self, r: dict) -> list[str]:
        """Return the ordered cell strings for one result row,
        matching the column order in ``_populate_table``."""
        # Acquisition time from DICOM StudyTime (HHMMSS)
        st = r["study_time"]
        acq = f"{st[:2]}:{st[2:4]}:{st[4:6]}" if len(st) >= 6 else st
        # Download end = timestamp from DB
        end_str = r["timestamp"]
        # Download start = end - duration
        try:
            end_dt = datetime.fromisoformat(end_str)
            start_dt = end_dt - timedelta(seconds=r["duration_seconds"])
            start_str = start_dt.strftime("%H:%M:%S")
            end_short = end_dt.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            start_str = "—"
            end_short = end_str
        return [
            acq,
            start_str,
            end_short,
            r["source_pacs"],
            r["modality"],
            r["study_description"],
            r["series_description"],
            str(r["image_count"]),
            f"{r['duration_seconds']:.1f}",
            f"{r['estimated_mbps']:.2f}",
        ]

    def _check_resend(self, results: list[dict],
                      baseline: dict | None) -> None:
        if not results or not baseline:
            return
        threshold = (baseline["median"]
                     - RESEND_STDDEV_MULTIPLIER * baseline["stddev"])
        for r in results:
            if r["estimated_mbps"] > 0 and r["estimated_mbps"] < threshold:
                self.lbl_resend_warning.setText(
                    "Warning: One or more series have unusually low transfer "
                    "speeds. Series were likely resent (nachgesendet) after "
                    "initial acquisition.")
                return
