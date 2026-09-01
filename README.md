# DICOM Sync GUI

Cross-platform DICOM transfer tool with a real-time dashboard.
Automatically downloads all series from configured source PACS within a
configurable time window — no manual study selection required.

---

## Standalone App (macOS)

A ready-to-run macOS app is available on the
[Releases](https://github.com/braegel/dicom_sync_gui/releases) page —
no Python installation required.

| File | Architecture | Size |
|---|---|---|
| [`DICOM_Sync_1.6.0_macOS_arm64.dmg`](https://github.com/braegel/dicom_sync_gui/releases/download/v1.6.0/DICOM_Sync_1.6.0_macOS_arm64.dmg) | Apple Silicon (M1/M2/M3/M4) | ~59 MB |
| [`DICOM_Sync_1.6.0_macOS_x86_64.dmg`](https://github.com/braegel/dicom_sync_gui/releases/download/v1.6.0/DICOM_Sync_1.6.0_macOS_x86_64.dmg) | Intel | ~63 MB |

**Installation:**

1. Open the `.dmg` file.
2. Drag **DICOM Sync** into the **Applications** folder.
3. On first launch: right-click → **Open** → **Open** to bypass Gatekeeper
   (the app is not code-signed).

The app is self-contained and stores its configuration in
`~/Library/Application Support/DicomSyncGUI/dicom_sync_config.json`.

> To build the app yourself, see [Building the standalone app](#building-the-standalone-app) below.

---

## Features

- **Multiple Source PACS** — configure any number of remote PACS, each with
  its own AE title, IP, port, transfer syntax, retrieve method (C-MOVE or
  C-GET), and *its own* local destination (AE title, port, fallback folder).
- **Automatic Service** — start/stop a continuous download loop that queries,
  compares, and transfers all new series.
- **Prior Studies** — optionally download the last N prior studies per patient
  (all modalities or same modality only).
- **Institution Filter Groups** — create named groups, assign institutions,
  and select which groups appear on the dashboard. Unknown institutions are
  always downloaded and trigger a popup with sound alert.
- **UI Localization** — English, German, French, Spanish. Pick the language
  in Settings → General. The "Copy" button in the Download Completions window
  writes its clipboard text in the configured language (e.g. German:
  `Abschluss Bildübertragung: HH:MM:SS`) so it can be pasted directly into
  a radiology report.
- **Retry Blacklist** — series that repeatedly fail to arrive are
  automatically skipped after 3 fruitless attempts. This covers both a
  C-MOVE that fails outright (e.g. a tiny localizer the source PACS
  refuses to send) *and* one the source reports as successful that still
  leaves nothing new in the local PACS — the case of an object the local
  PACS silently discards, such as an OsiriX ROI/Annotation SR, which
  would otherwise be re-fetched on every single cycle forever. Attempt
  counts persist across restarts; the counter resets as soon as the
  series actually gains instances locally.
- **Custom Notification Sound** — plays a two-tone chime when a patient's
  studies complete downloading. Optionally select a custom WAV file or disable
  sound entirely. With active filter groups, sound only plays for matching
  institutions. A descending "sad" tone plays when transfer speed is below the
  red threshold.
- **Real-Time Dashboard** —
  - Series queue with Patient, Study, Series, Modality, Images, Pending,
    img/min, Status, and cumulative ETE (estimated time to end).
  - Throughput statistics: Last Series, Median 5, Median 10, Median All
    (images/minute), colour-coded relative to overall median.
  - Study rate display showing studies/hour from PACS queries, with a
    high-load popup at ≥12 studies/hour.
  - Series with fewer than 10 images are excluded from speed statistics.
- **Download Completions Window** (View → Download Completions) — live log
  of completed studies with Patient, Study Description, Institution, Time
  Acquired, Download Completed, Download Duration, and Delay columns. Delay
  cells are ±1σ colour-coded, Download Duration cells are ±2σ colour-coded,
  and each row has a per-row Copy button that puts
  `Image transfer completed: HH:MM:SS` (localized) on the clipboard.
- **Transfer Performance Statistics** (View → Transfer Performance, Ctrl+T) —
  summary metrics, per-source and per-modality breakdown tables, and a
  Mbit/s boxplot aggregatable by hour/day/week/month (bucketed by actual
  download time, not DICOM acquisition date). Sourced from the SQLite
  transfer log.
- **Examination Lookup** (Tools → Examination Lookup) — enter cleartext
  PatientID / AccessionNumber / StudyDate to find transfer details for a
  specific examination. Warns when series were likely resent (Mbit/s is a
  low outlier).
- **Transfer Performance Log** — every series and study transfer is recorded
  in a local SQLite database (patient-identifiable fields SHA-256 hashed)
  for regulatory compliance documentation (StrlSchV / DIN 6868-159).
- **Built-in Storage SCP** — automatic fallback per source PACS when the
  configured local destination is not reachable; images are saved to a
  configurable folder.  It can also be switched on deliberately per
  source (*Receive C-MOVE images with the built-in SCP*) for a local
  PACS that is reachable but still rejects that source's images — see
  the 1.5.0 changelog entry.  It is also measurably faster than letting
  a PACS receive the C-STORE itself, because it writes a file instead
  of doing per-image database work on the transfer's critical path.
- **Local Inventory Query** — the endpoint asked "what has already
  arrived?", which is what stops the engine re-downloading series it
  already has.  Normally the local PACS answers this as well as
  receiving the images, so the per-source fields stay empty; with the
  built-in Storage SCP receiving, they default to `127.0.0.1` because
  the SCP can otherwise bind the very address the query uses and it
  answers Storage only.
- **Filter Groups Export/Import** — back up or share institution assignments
  as JSON (merge or replace mode).
- **Dark Theme** — modern dark UI, platform-independent via PySide6/Qt.
- **Log Window** — accessible via View → Show Log Window; supports clear and
  save-to-file.

---

## Installation (Windows / Linux — from source)

> **macOS users:** use the standalone app from the
> [Standalone App (macOS)](#standalone-app-macos) section above.
> No Python installation is needed.

### Requirements

| Dependency   | Minimum version |
|---|---|
| Python       | 3.10+           |
| PySide6      | 6.5+            |
| pydicom      | 2.4+            |
| pynetdicom   | 2.0+            |

### 1. Install Python

#### Windows

Download the installer from <https://www.python.org/downloads/windows/> and
run it.  **Check "Add Python to PATH"** during installation.

Alternatively, via winget:

```powershell
winget install Python.Python.3.12
```

#### Linux (Debian / Ubuntu)

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv
```

#### Linux (Fedora / RHEL)

```bash
sudo dnf install python3 python3-pip
```

#### Linux (Arch)

```bash
sudo pacman -S python python-pip
```

> **Note:** On some Linux distributions PySide6 requires additional system
> packages for Qt. If you see errors about missing libraries, install:
>
> ```bash
> # Debian / Ubuntu
> sudo apt install libegl1 libxkbcommon0 libxcb-cursor0 libxcb-icccm4 \
>     libxcb-keysyms1 libxcb-shape0
>
> # Fedora
> sudo dnf install mesa-libEGL libxkbcommon xcb-util-cursor xcb-util-keysyms
> ```

---

### 2. Clone or download the project

```bash
git clone https://github.com/braegel/dicom_sync_gui.git
cd dicom_sync_gui
```

---

### 3. Create a virtual environment (recommended)

#### Linux

```bash
python3 -m venv venv
source venv/bin/activate
```

#### Windows (PowerShell)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

#### Windows (CMD)

```cmd
python -m venv venv
venv\Scripts\activate.bat
```

---

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

This installs PySide6, pydicom, and pynetdicom.

---

## Running the application (from source)

```bash
# Direct
python main.py

# As module
python -m dicom_sync_gui
```

On first launch the settings dialog opens automatically so you can configure
your local and source PACS.

---

## First-time setup

1. **Local PACS** — set the AE title, IP address, and port of your local
   receiver. Click "Auto-detect IP" to fill in the current machine's address.
2. **Source PACS** — fill in the fields for a remote PACS and click "Add New".
   Repeat for each source. Each source can use C-MOVE or C-GET as retrieve
   method.
3. **General** — configure prior studies (count and same-modality option).
4. **Fallback storage** — optionally enable "Download to folder if local PACS
   is not available" and choose a directory.

After saving, the dashboard is ready. Set the desired time window, max
images per series, and query interval, then click **Start Service**.

---

## Filter groups

Open **Settings → Manage Filter Groups** to:

- Create named groups (e.g. "Clinic A", "MRI Centre").
- Query source PACS to discover institution names.
- Assign each institution to exactly one group.
- Export/Import group configurations as JSON.

On the dashboard, enable filtering and select which groups to include. Studies
from unknown institutions are always downloaded; a popup with sound alerts you
so you can assign them.

---

## Notification sounds

When all series of a study have been downloaded, a notification sound plays.
The default is a built-in two-tone chime (A5 + D6). Sound settings are
**per source PACS**:

- **Enable/disable** per source via the checkbox in the Download Service panel.
- **Custom WAV file** per source via Settings → PACS Configuration →
  Notification Sound.

If filter groups are active, the sound only plays for institutions that belong
to an active filter group.

---

## Running tests

The project includes a comprehensive test suite (744 tests).

```bash
# Linux / macOS (headless — no display required)
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -v

# Windows (PowerShell)
$env:QT_QPA_PLATFORM="offscreen"; python -m pytest tests/ -v

# Windows (CMD)
set QT_QPA_PLATFORM=offscreen && python -m pytest tests/ -v
```

> Offscreen mode tests all logic, signal wiring, and widget state but does
> not render pixels on screen. Visual layout must be verified manually.

---

## Building the standalone app

The macOS `.app` bundle is built with [PyInstaller](https://pyinstaller.org/).
The release ships both architectures, each built from its own dedicated
virtual environment so the Mach-O target matches the host Python.

### Apple Silicon (arm64)

```bash
python3 -m venv venv               # arm64 Python (e.g. 3.14)
source venv/bin/activate
pip install -r requirements.txt pyinstaller

pyinstaller dicom_sync.spec --clean --noconfirm
# → dist/DICOM Sync.app  (arm64)
```

### Intel (x86_64)

Use an x86_64 Python (e.g. installed via `python.org` Universal2
installer, or downloaded specifically as `x86_64`). On Apple Silicon
hosts the x86_64 Python binary will automatically be invoked under
Rosetta.

```bash
python3 -m venv venv_x86           # x86_64 Python (e.g. 3.12)
source venv_x86/bin/activate
pip install -r requirements.txt pyinstaller

pyinstaller dicom_sync.spec --clean --noconfirm
# → dist/DICOM Sync.app  (x86_64)
```

### Package the DMG

```bash
ARCH=arm64   # or x86_64
mkdir -p _dmg
cp -R "dist/DICOM Sync.app" _dmg/
ln -s /Applications _dmg/Applications
hdiutil create -volname "DICOM Sync 1.6.0" \
  -srcfolder _dmg -ov -format UDZO \
  releases/DICOM_Sync_1.6.0_macOS_${ARCH}.dmg
rm -rf _dmg
```

> The DMG is not code-signed; first launch on another Mac needs
> right-click → **Open** to bypass Gatekeeper.
>
> DMGs are **not committed to the repository** — they are published
> exclusively as assets on the
> [GitHub Releases](https://github.com/braegel/dicom_sync_gui/releases)
> page (`gh release create vX.Y.Z --title … --notes-file … *.dmg`).
> The local `releases/` folder is just the build output directory and
> is git-ignored.

---

## Project structure

```
dicom_sync_gui/
├── main.py                         # Entry point, dark theme, dependency check
├── __init__.py                     # Package version (1.6.0)
├── __main__.py                     # python -m support
├── requirements.txt                # pip dependencies
├── dicom_sync.spec                 # PyInstaller build spec
├── pytest.ini                      # Test runner configuration
├── README.md
├── CHANGELOG.md                    # Release notes
├── LICENSE
├── .gitignore
│
├── assets/
│   └── AppIcon.icns                # macOS application icon
│
├── core/
│   ├── config.py                   # AppConfig, PacsNode, load/save
│   ├── dicom_ops.py                # C-ECHO, C-FIND, C-MOVE operations
│   ├── i18n.py                     # UI translations (en/de/fr/es)
│   ├── storage_scp.py              # Built-in DICOM Storage SCP
│   ├── transfer_engine.py          # Service loop, queue, stats, Qt signals
│   └── transfer_log.py             # SQLite transfer performance log
│
├── gui/
│   ├── main_window.py              # Main window, menus, engine wiring
│   ├── dashboard.py                # Dashboard: controls, queue, stats, sound
│   ├── settings_dialog.py          # PACS configuration dialog
│   ├── filter_groups_dialog.py     # Institution filter group editor
│   ├── unknown_institution_popup.py  # Alert popup for unknown institutions
│   ├── live_completions.py         # Download Completions window
│   ├── transfer_stats_window.py    # Transfer Performance Statistics window
│   ├── examination_lookup.py       # Examination Lookup dialog
│   ├── log_window.py               # Floating log viewer
│   └── styles.py                   # Shared button stylesheet constants
│
└── tests/                          # 744 tests
    ├── conftest.py                 # Shared fixtures
    ├── test_config.py
    ├── test_dicom_ops.py
    ├── test_transfer_engine.py
    ├── test_transfer_log.py
    ├── test_i18n.py
    ├── test_dashboard.py
    ├── test_settings_dialog.py
    ├── test_main_window.py
    ├── test_filter_groups_dialog.py
    ├── test_filter_groups_export_import.py
    ├── test_live_completions.py
    ├── test_transfer_stats_window.py
    └── test_examination_lookup.py
```

---

## Configuration file location

The configuration is stored as JSON in a platform-specific directory:

| Platform | Path |
|---|---|
| macOS    | `~/Library/Application Support/DicomSyncGUI/dicom_sync_config.json` |
| Linux    | `~/.config/DicomSyncGUI/dicom_sync_config.json` |
| Windows  | `%APPDATA%\DicomSyncGUI\dicom_sync_config.json` |

A log file (`dicom_sync_gui.log`) is written to a platform-specific location:

| Platform | Log path |
|---|---|
| macOS    | `~/Library/Logs/dicom_sync_gui.log` |
| Linux    | `~/.local/state/dicom_sync_gui.log` |
| Windows  | `%APPDATA%\dicom_sync_gui.log` |

---

## License

This project is licensed under the **GNU General Public License v3.0**.
See [LICENSE](LICENSE) for the full text.

Copyright (C) 2026 Bernd Bragelmann
