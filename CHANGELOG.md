# Changelog

All notable changes to DICOM Sync GUI are documented in this file.

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
