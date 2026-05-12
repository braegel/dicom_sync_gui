# Changelog

All notable changes to DICOM Sync GUI are documented in this file.

## [1.0.8] — 2026-05-12

### Fixed (critical)
- `TransferEngine.queue_snapshot()` boundary: GUI thread no longer
  reads the engine's internal `_queue` directly; snapshot is taken
  under a lock so list reassignment in the service-loop thread
  can't race the GUI iterating it
- Single shared `TransferLog` across all source PACS engines so the
  per-instance write lock actually serializes writers (previously
  each engine had its own lock and writes could interleave)
- `QSoundEffect` is now a single lazily-created long-lived instance
  per dashboard; the previous create/`deleteLater` per notification
  could race PySide6 dealloc when study completions arrived in
  quick succession
- `closeEvent` joins engine threads in 100 ms slices with
  `processEvents()` between them so the "waiting for downloads"
  status bar stays responsive instead of freezing for 30 s × N
  sources
- `DicomOperations` C-ECHO / C-FIND / C-MOVE explicitly handle
  `None`/missing `Status` (dropped associations) instead of relying
  on the truthiness of `status and status.Status == 0x0000`
- `studies_queried` signal now emits an owned deep copy of the
  per-cycle dict, eliminating an implicit ownership contract
  between engine and GUI
- Atomic config save: `AppConfig.save()` writes to a `.tmp` sibling
  and `os.replace`s onto the real path so a crash mid-write cannot
  truncate the config and re-trigger the first-run setup wizard
- `TransferEngine` now calls `AE.shutdown()` in its `finally` block
  so pynetdicom reactor threads exit deterministically on stop
  instead of accumulating
- `StorageSCP.running` is now a thread-safe property guarded by a
  dedicated lock; `start()` compare-and-sets and `stop()` atomically
  claims the shutdown so the reactor's own `finally` cannot race
  an external `stop()` into a double `ae.shutdown()`

### Changed
- Notification WAVs are pre-generated at app startup via
  `QTimer.singleShot(0, …)` so the first study-completion sound is
  instant instead of paying ~30 k samples of Python DSP synchronously
  on the GUI thread
- `config.save()` calls from dashboard checkboxes/spinboxes are
  debounced through a 500 ms `QTimer` to coalesce bursts; immediate
  flush on Start-clicked and on window close so no changes are lost
- `_resolve_priors` (was 100+ lines, 4 levels of nesting) split into
  `_resolve_priors_for_patient`, `_filter_priors_by_modality`,
  `_build_prior_jobs_for_study`
- Round-trip unknown JSON keys in `AppConfig.load()`/`save()` so a
  future build's new setting is not silently dropped when an older
  version saves
- `TransferLog.mbps_stats()` computes the resend-detection baseline
  in SQL instead of pulling the whole `series_transfer` table into
  Python on every Examination Lookup search
- `TransferStatsWindow` keeps a single `TransferLog` open for the
  window's lifetime instead of creating/closing one on every filter
  change
- `StorageSCP` accepts a `bind_address` constructor arg (default
  still `0.0.0.0`)
- Dashboard threshold/colour magic numbers consolidated as module
  constants (`SPEED_BAND_RATIO`, `STUDY_RATE_*`, palette)
- `_log` is split from status-bar updates and routed through a
  `_log_message` signal so it's safe from any thread

### Internal
- `gui/styles.py` now owns `DARK_THEME` and a `_button_style(...)`
  factory used by all coloured buttons (replaced 8 near-duplicate
  inline strings)
- `pydicom` VR-UI warning is now scoped to the actual `send_c_find`
  call site via `warnings.catch_warnings()` instead of process-wide
- Narrowed `except Exception` to `except sqlite3.Error` around all
  `TransferLog.*` writes in `TransferEngine`
- Test suite: 722 passing (no count change; existing tests
  retargeted at new `queue_snapshot` and `transfer_log=` injection)

## [1.0.6] — 2026-04-11

### Added
- UI localization: English, German, French, Spanish
  - Language picker in Settings → General
  - Copy button in the Download Completions window produces its
    clipboard text in the configured language (e.g. German:
    "Abschluss Bildübertragung: HH:MM:SS") for pasting into reports
- Series retry blacklist: after 2 failed C-MOVE attempts for the
  same series, it is permanently excluded from future query cycles
  - Failure counts persist across app restarts in the SQLite
    transfer log (new `series_failures` table)
  - Successful transfers reset the counter
  - Stops the "stuck tiny series" re-download loop that previously
    could run every sync interval forever
- Download Completions window improvements:
  - New "Download Duration" column showing true wall-clock download
    time, color-coded red/green at ±2σ from the median
  - Per-row "Copy" button that copies
    "Image transfer completed: HH:MM:SS" (localized) to the clipboard
  - Columns auto-resize to fit header text so labels are never
    truncated (e.g. "Download Completed")
- Transfer Performance Statistics boxplot now buckets by the actual
  download timestamp rather than the DICOM acquisition date, so prior
  studies no longer skew historical buckets

### Fixed
- Quitting the app while a download was in progress no longer kills
  the in-flight C-MOVE — the engine thread is now joined (up to 30 s)
  so the current series completes and the SQLite transfer log stays
  consistent with what actually arrived on disk
- Local-PACS query failures in `_fetch_local_series_counts` were
  silently swallowed, causing the engine to treat the study as empty
  locally and re-download every series until the local PACS
  recovered. Failures are now logged at WARNING with the study UID
  so the root cause is traceable

### Changed
- Removed the stale `dicom_sync_gui/` duplicate package directory
  (March 2026 snapshot that had drifted ~5 weeks behind the active
  tree). No live code referenced it, but it could have been imported
  by accident or picked up by PyInstaller

### Internal
- Test suite grew from 613 to 689 tests (+76 covering retry
  blacklist, duration coloring, auto column width, copy button,
  localization, closeEvent join, local-query logging, and the
  `study_completed(fully_complete=True)` regression when a study
  contains a blacklisted series)

## [1.0.5] — 2026-04-06

### Added
- Mbit/s boxplot chart in Transfer Performance Statistics window
  - Aggregation by hour, day, week, or month
  - Responds to source/modality filters
- Examination Lookup dialog (Tools menu): enter PatientID / AccessionNumber /
  StudyDate in cleartext to find transfer details for a specific examination
  - Shows acquisition time, download start/end, duration, Mbit/s per series
  - Warns when series were likely resent (Mbit/s < median − 2σ)
- Download Completions window (View menu): live overview of completed studies
  - PatientName, StudyDescription, Institution, acquisition time, download
    time, and delay (acquisition → download)
  - Median delay label, color-coded delay cells (red > median + 1σ,
    green < median − 1σ)

## [1.0.4] — 2026-04-06

### Added
- Transfer Performance Statistics window (View menu, Ctrl+T)
  - Summary metrics, per-source and per-modality breakdown tables
  - Filterable study- and series-level detail tables
  - Data sourced from the SQLite transfer log
- Transfer log integration: series and study transfers are now recorded
  automatically during download
- Sad notification sound when transfer speed is below threshold
  (descending tone instead of ascending)
- AccessionNumber tracked per series for examination traceability

### Changed
- Notification sound only plays when a study is fully downloaded
  (not for partial downloads via small-series filter exception)

### Fixed
- Segfault when closing Filter Groups dialog during background institution
  query (QDialog destroyed on wrong thread)
- Study-level transfer log used incorrect image count and reconstructed
  duration instead of actual measured values

## [1.0.3] — 2026-04-05

### Added
- Transfer performance log (SQLite) for regulatory compliance documentation
  (StrlSchV § 123 / DIN 6868-159)
  - Per-series and per-study transfer metrics (image count, duration,
    estimated bandwidth)
  - Patient-identifiable fields (PatientID, AccessionNumber, UIDs) stored
    as SHA-256 hashes — traceable with original identifiers, no PII on disk
  - Approximate byte estimation per modality (CT, MR, CR, DX, US, PT, NM,
    MG, XA) for Mbit/s calculation
  - Filterable queries by date range, source PACS, modality, patient,
    and accession number
  - SQLite database at platform-standard location for easy external analysis

## [1.0.2] — 2026-04-05

### Added
- Per-PACS notification sound settings — enable/disable and choose a custom
  WAV file independently for each source PACS
- Sound notification checkbox in the Download Service panel (per source tab)
- Notification sound file selector in PACS Configuration dialog
- x86_64 macOS DMG for Intel Macs

### Changed
- Notification sound triggers per completed **study** (all series finished),
  not per patient
- Each study's institution is filtered independently against the active
  filter group
- Replaced global `study_complete_sound_enabled` / `study_complete_sound_path`
  config with per-PACS fields on `PacsNode`

### Fixed
- Prior studies now respect institution filter groups — priors from an
  inactive group are no longer downloaded
- Thread safety: `StorageSCP.images_received` is now lock-protected;
  `running` flag deferred to actual thread start
- UI no longer blocks during PACS institution discovery queries
  (moved to background thread)
- Association leak in `c_echo` — now released in `try/finally`
- Socket leak in `get_local_ip()` — now uses context manager
- Log file written to `~/Library/Logs/` (macOS) instead of working directory
- `_pending_start_params` is now a dict keyed by remote_key, preventing
  race condition when starting two sources quickly

## [1.0.1] — 2026-04-04

### Added
- Standalone macOS app (.app bundle via PyInstaller)
- Custom notification sound — default two-tone chime (A5 + D6) or custom WAV
- GitHub Actions CI for automated arm64 DMG builds on release

### Changed
- Notification sound plays per completed study instead of using `QApplication.beep()`

### Fixed
- Prior studies bypassing institution filter groups

## [1.0.0] — 2026-03-15

Initial release.

- Multiple source PACS with independent download services
- Real-time dashboard with queue, ETE, and throughput statistics
- Institution filter groups with auto-discovery and unknown-institution alerts
- Prior studies download (same or all modalities)
- Built-in Storage SCP fallback when no local DICOM server is reachable
- Filter groups export/import (JSON, merge or replace)
- Dark theme, log window, C-ECHO test
- Cross-platform: macOS, Linux, Windows
