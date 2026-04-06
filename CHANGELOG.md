# Changelog

All notable changes to DICOM Sync GUI are documented in this file.

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
