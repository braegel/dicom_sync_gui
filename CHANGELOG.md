# Changelog

All notable changes to DICOM Sync GUI are documented in this file.

## [1.5.1] — 2026-08-21

Maintenance release from a full-project code review (21 findings).  Two
of them were real defects; the rest is technical debt paid off without
any change in behaviour.

### Fixed
- **Prior studies were silently discarded when the modality filter was
  on.** `ModalitiesInStudy` arrives from pydicom as a `MultiValue` when
  a study has more than one modality, and stringifying it yields
  `"['CT', 'SR']"` — so the split produced the tokens `['CT'` and
  `'SR']` and nothing ever matched.  A current study with two
  modalities (the common case: `SR\CT`) therefore threw away EVERY
  prior.  Single-modality studies worked, which is why it went
  unnoticed.
- **The dashboard's queue-status helpers were dead code that made the
  test suite lie.**  `_status_text` / `_status_color` were defined
  twice in the same class; the second pair silently shadowed the
  delegation to `gui.queue_table`.  Nothing in the app called either
  copy, but the tests did — so a regression in the code that actually
  renders the queue would have passed.  The queue's status colours are
  covered for the first time.

### Changed
- The **debounced config autosave no longer runs on the GUI thread.**
  `save()` fsyncs, and doing that inside a Qt slot stalled the event
  loop for the length of the disk write.  Writes now happen on a
  background thread from a snapshot, coalescing bursts to a single
  write of the final state.
- **The transfer log's lookup columns are indexed.**  Measured on a
  58 757-row log: a patient lookup drops from 8.7 ms to 0.0 ms, the
  median scan from 28.2 ms to 9.7 ms.  `source_pacs` and `modality` are
  deliberately left unindexed — measured at 112.9 ms → 114.5 ms, i.e.
  no gain, because they match most of the table.
- The engine's **inter-cycle wait blocks on the cancel event** instead
  of polling it once a second, so Stop takes effect immediately.
- Shutdown now **closes the shared SQLite connection** (checkpointing
  the WAL) and **destroys outgoing dashboards explicitly** — with the
  cyclic GC disabled on Python 3.14+, a detached dashboard caught in a
  reference cycle kept five QTimers running for the rest of the
  session.

### Internal
- New pure, Qt-free modules `core/prior_studies.py` and
  `core/institution_filter.py`, following the pattern of
  `core.queue_planner`.
- Every remaining missing type annotation filled in (40), and verified
  to *resolve* rather than merely parse — Python 3.14 defers annotation
  evaluation, so a missing typing import is invisible here but a hard
  `NameError` on the Python 3.12 the release DMG is built with.
- No function nests deeper than three levels; the deprecated
  `gui.dashboard` re-export blocks are gone.
- 1233 tests (was 1178).

## [1.5.0] — 2026-08-21

A source PACS that sends JPEG 2000 pixel data no matter what was
negotiated could not be downloaded at all: every image series failed
while its reports came through.  DICOM Sync can now take delivery of
such a source's images itself and hand the local PACS files it accepts.

### Added
- **Receive C-MOVE images with the built-in SCP** — a new per-source
  option in the PACS node editor.  Normally the source PACS delivers
  straight to the configured local destination; switch this on and
  DICOM Sync takes delivery instead and writes the images to the
  fallback folder, where the local PACS picks them up.

  Use it when the local PACS is reachable — it answers C-ECHO, queries
  work — but every C-MOVE from one particular source still comes back
  `0 completed / N failed`.  Seen in the field with a PACS that offers
  its images as *JPEG 2000 Lossless or Implicit VR Little Endian* but
  sends JPEG 2000 compressed pixel data either way.  A receiver that
  accepts the uncompressed option then gets objects whose pixel data
  does not match their declared syntax, and rejects every one of them.

  The SCP binds only the network interface that reaches that source, so
  the local PACS keeps serving its own address for everything else,
  including the queries DICOM Sync uses to track what has arrived.
  Requires a fallback folder.

### Fixed
- **The built-in Storage SCP accepted only uncompressed transfer
  syntaxes.** It offered pynetdicom's defaults, so against the kind of
  source described above it would have stored mislabelled images
  instead of usable ones.  It now prefers lossless compressed syntaxes
  when a sender offers them.  Lossy syntaxes are still accepted, but
  ranked behind the uncompressed ones — a sender offering both can
  serve either, and picking lossy would discard image data for nothing.
- **Images stopped arriving once the local PACS emptied the fallback
  folder.** The folder is watched by a local PACS that imports what
  lands in it and then removes the emptied directory; every image after
  that first import failed until the service was restarted.
- **"Preferred Syntax" under Local Destination did nothing.** It was
  saved to the configuration and never read.  It is the receiving
  side's preference, so it only ever had a meaning for the built-in
  SCP — it now drives it, and its tooltip says so.

### Internal
- `pytest` is declared in `requirements.txt`; it is excluded from the
  app bundle by the PyInstaller spec.
- 20 new tests (1178 total).

## [1.4.0] — 2026-08-14

Maintenance release from two full code reviews (65 findings).  No new
features: every change either fixes a defect, removes a way for the GUI
to lie about its state, or breaks up code that had grown too large to
review safely.

### Fixed
- **Statistics window went blank after being closed once.** Closing it
  released the SQLite connection, but the window is cached and re-shown,
  so every later query failed silently and the user saw empty tables.
- **Warning dialogs no longer freeze the app.** The PACS-unreachable and
  connection-lost warnings ran a nested event loop inside a signal slot,
  re-entering every other queued engine signal.  Both are now non-modal
  and de-duplicated per source.
- **A failed built-in receiver is now reported.** When the local PACS is
  unreachable and the fallback Storage SCP cannot bind, the dashboard
  used to keep showing "Starting service…" forever with only a log line.
  It now returns to "Stopped" and explains why.
- **Truncated series queries can no longer hide a study forever.**
  Deferring a study's completion verdict when its series query broke off
  mid-stream was unbounded, so a persistently flaky source kept the study
  out of Download Completions indefinitely.  Capped at three consecutive
  truncations.
- **Config files containing non-ASCII now load on any locale.** The
  config was read with the platform default encoding; on a German
  Windows box a UTF-8 config failed to load, silently triggering the
  first-run wizard and dropping unknown keys on the next save.
- **Transfer-rate standard deviation is no longer numerically unstable.**
  Measured relative error on a fast link improved from ~1.2e-10 to
  ~1.8e-16.

### Changed
- The **Download Completions** list and the **statistics** detail tables
  are capped (500 rows each), oldest first.  The statistics summary,
  boxplot and per-source/per-modality breakdowns still use the full
  history — only the browsable tables are limited, with a row count
  shown.
- The **high study load** warning has a 15-minute cooldown; a rate
  hovering at the threshold used to re-pop it every few seconds.
- The built-in **Storage SCP** binds in the calling thread and fails
  immediately instead of sleeping on the GUI thread and guessing.
- **Source PACS settings are validated** on add/save: AE titles are
  capped at DICOM's 16 characters and the IP field must be a valid
  address or hostname.
- The **automatic-GC workaround** is gated on the interpreter version and
  logs once at startup, so it lifts by itself on a fixed CPython.
- Remaining hardcoded German strings now go through the translation
  layer (five new keys in all four languages).

### Internal
- New `gui/queue_table.py` — the series-queue rendering, extracted from
  the dashboard.
- New `gui/service_watchdog.py` — active-transfer state, the stall
  watchdog's two-condition wedge rule, and pending-restart bookkeeping.
  Eight loose widget attributes became two small objects.
- New `core/dicom_time.py` — one implementation of DICOM date/time
  parsing and display formatting, replacing four hand-rolled copies (one
  of which was dead code kept alive only by its tests).
- `SourceDashboard` shrank from 1607 to 1234 lines; the longest function
  in the project went from 160 to 75 lines; nesting deeper than three
  levels went from 6 sites to 4; unannotated parameters from 11 to 0.
- Per-study bookkeeping in the transfer engine is now pruned, so a
  service running for weeks no longer grows three dictionaries without
  bound.
- Colours live in `gui/styles.py`; no inline hex remains outside the
  palette modules.
- `AppConfig.local_node`'s setter used to accept an assignment and
  silently discard it; it now raises.

### Tests
- 970 → 1158 (+188), including three new suites for the extracted
  modules.

## [1.3.3] — 2026-06-29

Follow-up to 1.3.2: keeps straggler images from falsifying the Download
Completions timestamp.

### Fixed
- **"Download Completed" time only advances on a real wave of new
  images.** 1.3.2 re-signalled a study's completion whenever *any* new
  image arrived, so single-image retries and re-sends (1–5 images)
  moved the row's completion time — and the Copy button behind it — to
  the straggler's arrival, falsifying it.  A re-completion now refreshes
  the timestamp only when more than 5 new images have arrived since the
  last update; the first completion of a study still always registers.

## [1.3.2] — 2026-06-28

Bugfix: the Download Completions "Copy" button kept the timestamp of a
study's first completion even when more images of that study arrived
later.

### Fixed
- **Copy button timestamp now advances to the last image arrival.**
  A study was only ever signalled complete once per cycle, so its
  Download Completions row — and the per-row Copy button behind it —
  froze on the first completion's time.  When a late-arriving series
  (or a small series crossing the entry threshold) brings in more
  images, the study now re-signals completion: the row's "Download
  Completed" time advances, the Copy button captures the latest
  timestamp, and the completion chime sounds again.  Cumulative totals
  replace the row's image/duration values instead of double-counting.

## [1.3.1] — 2026-06-20

Stability patch: fixes the GUI freezing during downloads and the service
occasionally stopping on its own.

### Fixed
- **GUI freeze during downloads (root cause).** pynetdicom's DUL reactor
  thread polls every 1 ms; during a busy C-MOVE that pegged a CPU core,
  and the Python GIL then starved the Qt GUI thread so the window became
  unresponsive (confirmed with py-spy: the reactor was the only active
  Python thread while the GUI sat idle in the event loop).  Each
  association now raises the reactor poll delay to 20 ms, freeing the GIL
  for the GUI; transfer latency is unaffected in practice.
- **Stall watchdog restarted the service spuriously.** The old
  "per-image, 10 s" model fired on healthy large series (a 300-image 3D
  MR can send no per-image C-MOVE status for far longer than 10 s).
  Replaced with a series-level deadline derived from the series' image
  count and the measured rate (generously clamped); an auto-restart now
  fires only when a series both overruns that deadline AND shows no
  progress for two minutes.
- **Fallback Storage SCP flooded the GUI.** When the built-in SCP
  receives images to a folder, it emitted a cross-thread signal (with a
  full dataset) per image; on a large transfer that flooded the GUI
  event queue.  Throttled to the first image + every 25th, carrying just
  the running count.
- **Service could end up silently "Stopped"** after a watchdog restart if
  the engine was still winding down a wedged C-MOVE: the 60 s restart
  safety window expired before the engine's 300 s DIMSE timeout. The
  safety window is now longer than the DIMSE timeout and forces a fresh
  start instead of giving up; a transient reachability probe failure
  during an auto-restart now retries instead of stopping.

### Changed
- The series queue table no longer auto-resizes data columns to their
  contents on every update (fixed/interactive widths), removing a
  needless per-update relayout cost on large queues.

### Tests
- 951 → 956 (DUL reactor delay, series-level watchdog, SCP throttle,
  restart-safety forced start, table-update performance).

## [1.3.0] — 2026-06-18

Transfer resilience, queue prioritization, and a full follow-up code
review pass.

### Added
- **Per-image stall watchdog**: if no image of a series arrives for
  10 s, a sad chime + a non-blocking notice fire and the service
  auto-restarts to unblock a wedged C-MOVE.  The watchdog re-arms on
  every image, so a merely slow large series never trips it.
- **PACS-unreachable auto-restart loop**: when the watchdog restart hits
  a source PACS that is briefly unreachable, the service keeps retrying
  every 5 s — with a distinct siren alarm — until the PACS answers,
  instead of silently staying stopped.  Stop / quit cancel the loop.
- **First-substantial-series fast-lane**: the first series of every study
  with more than 10 images is pulled before all other series, across
  studies and patients, so a reader gets one viewable series of every
  pending study as early as possible (ranked above the axial fast-lane).
- **"Series Created" column** in the Series Queue, showing each series'
  acquisition date/time (DICOM SeriesDate/SeriesTime, study date/time
  fallback), formatted `DD.MM.YYYY HH:MM`.
- **Live Pending / ETE countdown**: both columns now count down each
  second during an active transfer.

### Fixed
- **UI freeze during large downloads**: the per-second live refresh
  reallocated a table item for every queue row every tick (across all
  source tabs), pinning the GUI thread; it now updates cell text in
  place and skips hidden tabs entirely.
- **GUI-thread CPU spin on large series**: `series_progress` was emitted
  once per image (a queued cross-thread signal per image flooded the
  event loop); it is now throttled to ~2/s with a guaranteed final emit.
- **Duplicate "study complete" chime**: an already-local study that got
  re-queried (or re-run after a restart) chimed every cycle; it now
  chimes only the first time it completes.
- **Series that can't be retrieved are retried at most 3 times**, then
  shown as "⊘ Not available" in the queue instead of being retried
  forever; they no longer block study/patient completion.
- **Auto-restart notice no longer blocks the service**: a single,
  self-dismissing, non-modal notice replaces the stacking modal dialogs.

### Changed
- The Download Completions Copy button resolves its localized prefix at
  click time and follows a language change via `set_language()`.

### Internal
- Extracted the pure queue-ordering logic (axial / first-substantial /
  priority-term sort) into a new Qt-free `core.queue_planner` module.
- Code-review pass: `_queue` reads now honor the `_queue_lock` contract;
  `TERMINAL_STATUSES` is a single shared constant; several god-methods
  (`_check_study_complete`, `_on_scp_check_done`, `DicomOperations`
  `__init__`, `config.load`, `main`, and five dialog `_setup_ui`
  builders) split into focused helpers; swallowed exceptions are logged.

### Tests
- 912 → 945 (per-image throttle, retry/unavailable status, series
  date/time, siren generation, auto-restart retry loop, live-refresh
  performance, queue-planner extraction).

## [1.2.0] — 2026-06-11

Full-project code review: 5 critical, 14 medium and 14 low findings
resolved across three passes.

### Fixed
- **Query window honors the configured hours**: the C-FIND date range
  was hardcoded to yesterday–today, so "Download last" values beyond
  ~24–48 h silently found nothing older than yesterday.  The range now
  reaches back to the cutoff date.
- **Editing a source in Settings no longer wipes its priority series
  terms and notification-sound preference** — the editor carries over
  fields it has no widgets for instead of resetting them to defaults.
- **A hung PACS can no longer wedge the service forever**: explicit
  ACSE (30 s) / DIMSE (300 s) / network (330 s) timeouts on every
  association (pynetdicom's default DIMSE timeout is *wait forever*).
- **Cross-midnight delay** in Download Completions: a study acquired
  at 23:55 and completed at 00:05 showed a bogus ~23 h delay (and
  skewed the colour bands); the delay now wraps correctly.
- **`mbps_stats` median** for even row counts now averages the middle
  pair (was: upper element), consistent with every other median.

### Changed
- **C-GET option removed** from Settings: it was never implemented
  (the engine always issues C-MOVE).  The stored config field
  round-trips, so old configs load unchanged.
- **Configured transfer syntax** is now offered as the *preferred*
  syntax on the Q/R presentation contexts (LE fallbacks kept, so
  interoperability cannot regress).
- **Statistics window and Examination Lookup query SQLite on a worker
  thread** — no more GUI freeze on large transfer logs; the Search
  button disables during an in-flight query.
- **Unknown-institution popup is non-modal** and deduplicated per
  institution; multiple alerts no longer stack modal dialogs.
- **Series queue table updates in place** during downloads (row
  selection survives, much less flicker); a full rebuild happens only
  when the queue composition changes.
- **Fallback mode logs receive progress** (first image, then every
  25th) so the built-in Storage SCP is no longer silent.
- **Log window** caps retention at 5 000 lines and counts lines in
  O(1) — previously unbounded memory and quadratic cost over a 24/7
  run.  The app's file log now rotates (2 MB × 3 backups).

### Internal
- Every `DicomOperations` owner shuts its pynetdicom AE down via the
  new `close()`; config load/save no longer raise into the Qt event
  loop; `TransferStats` is lock-protected for cross-thread reads;
  `StorageSCP.save_as` is pydicom 2.x/3.x compatible.
- `SourceDashboard` slimmed: WAV synthesis → `gui/notification_sound`,
  studies-per-hour logic → `gui/study_rate` (pure functions),
  `_setup_ui` split into seven per-section builders.
- Six hand-rolled median/stddev implementations consolidated into
  `core/stats_utils` (stdlib `statistics` underneath).
- `PacsNode` is a dataclass; dead `is_local` editor mode removed;
  filter-group export/import logic deduplicated into shared helpers;
  ~170 GUI method signatures gained type hints.

### Tests
- 799 → 875 (engine query window, settings round-trip, timeouts,
  thread-safety hammer, midnight wrap, log retention, incremental
  table updates, pure study-rate/stats helpers, and more)

## [1.1.0] — 2026-06-04

### Added
- **Per-source priority series loading** (Settings → Manage Priority
  Series…): studies with at least one series matching a configured
  term/regex float to the top of the download queue.  Each entry is a
  case-insensitive substring or full regex; list order = priority;
  pre-populated with typical neuro/vascular terms (cct, cta, ct-a,
  angio, nevas, perf, perfusion, ctp, ct-p).  A source-PACS picker in
  the dialog keeps a separate list per remote.

### Fixed
- `StorageSCP.start()` raises `RuntimeError` when the bind fails
  instead of silently reporting success; the engine launch is skipped
  so C-MOVEs never fire at a dead local SCP.
- C-FIND / C-MOVE associations are released via try/finally — a DIMSE
  failure mid-stream no longer leaves the TCP association dangling.
- FilterGroupsDialog institution discovery uses each remote's own
  local AE (was broken on multi-source setups with different AEs).
- Assorted smaller fixes: TransferLog bool/int guard, study-rate
  label leak, log-save OSError handling, editor-delegate commit on
  source switch.

### Tests
- 785 → 799

## [1.0.11] — 2026-05-22

### Added
- **Download Completions: Copy button moved to the first column** so
  it remains reachable on narrow windows without horizontal scrolling
- **Sortable column headers** in the Download Completions table —
  click any column title to sort ascending; click again for
  descending
- **Aggregation by study_uid**: when late-arriving series for an
  already-completed study come in during a later query cycle, the
  existing row in the Download Completions window is *updated*
  (image_count and download_duration_seconds summed, completed_time
  advanced to the latest, Copy button rebound to the new
  timestamp) instead of a duplicate row being appended.  Different
  studies still get separate rows.

### Changed
- **Studies with fewer than 10 downloaded images are filtered out**
  of the Download Completions window (stray priors, partial fetches
  and isolated tiny series no longer clutter the list).  Threshold
  lives in `MIN_IMAGES_FOR_COMPLETIONS_ENTRY` in `gui/main_window.py`

### Internal
- Per-row state in the completions table (study_uid, image_count,
  download_duration, delay) now lives on the `QTableWidgetItem`s
  themselves via `Qt.UserRole`; the parallel lists `_delays`,
  `_durations`, `_study_uids` are gone.  Aggregation lookup and
  stat-colour helpers are now sort-stable.
- Magic column indices replaced with module constants
  (`_COL_COPY`, `_COL_PATIENT`, …, `_COL_DELAY`).

### Tests
- 726 → 744 (+18 covering the cutoff, copy-in-first-column,
  sortable headers, aggregation, position stability, and the
  ~21-image notification-sound threshold boundary)

## [1.0.10] — 2026-05-14

### Changed
- Notification sound is suppressed when a study's total downloaded
  image count is below `MIN_IMAGES_FOR_NOTIFICATION_SOUND = 21`
  (typically isolated small series, stray priors, or partial fetches
  that would otherwise spam the user with chimes)
- `study_completed` signal now carries the total downloaded image
  count as a fourth argument so the dashboard can apply the new
  size threshold without consulting the engine queue

### Tests
- 724 → 726 (+`test_no_sound_when_fewer_than_21_images_downloaded`,
  +`test_sound_when_exactly_21_images_downloaded` for the boundary)

## [1.0.9] — 2026-05-14

### Fixed
- **Download Completions barely getting filled**: `fully_complete`
  was checking `done_count` against the full count of series the
  PACS returned, including filtered-out ones (institution filter,
  `max_images` filter, small-series exception). A study with even
  one filtered-out series could never reach the completion entry.

  New rule: a study is fully complete when every *queued* series of
  the study has `status == "done"`. Filter-rejected series are not
  in the queue and so are naturally ignored. Series with fewer than
  6 remote images may additionally be `error` / `skipped` without
  blocking the completion — this covers stuck tiny localizers that
  the PACS refuses to send.

### Internal
- `_study_total_series` dict removed from `TransferEngine` — it was
  the backing store for the old `total_remote` comparison and is no
  longer needed
- Tests: 724 passing (+2 covering the new small-series-failure-OK
  path and the filtered-queue completion path)

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
