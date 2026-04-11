# Changelog

All notable changes to DICOM Sync GUI are documented in this file.

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
