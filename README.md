# DICOM Sync GUI

Cross-platform DICOM transfer tool with a real-time dashboard.
Automatically downloads all series from configured source PACS within a
configurable time window — no manual study selection required.

---

## Features

- **Multiple Source PACS** — configure any number of remote PACS, each with
  its own AE title, IP, port, transfer syntax, and retrieve method (C-MOVE or
  C-GET).
- **Automatic Service** — Start/Stop a continuous download loop that queries,
  compares, and transfers all new series.
- **Prior Studies** — optionally download the last N prior studies per patient
  (all modalities or same modality only).
- **Institution Filter Groups** — create named groups, assign institutions,
  and select which groups appear on the dashboard. Unknown institutions are
  always downloaded and trigger a popup with sound alert.
- **Real-Time Dashboard** —
  - Series queue with Patient, Study, Series, Modality, Images, Pending,
    img/min, Status, and cumulative ETE (estimated time to end).
  - Throughput statistics: Last Series, Median 5, Median 10, Median All
    (images/minute), colour-coded relative to overall median.
  - Series with fewer than 10 images are excluded from speed statistics.
- **Built-in Storage SCP** — automatic fallback when no local DICOM server is
  reachable; images are saved to a configurable folder.
- **Filter Groups Export/Import** — back up or share institution assignments
  as JSON (merge or replace mode).
- **Dark Theme** — modern dark UI, platform-independent via PySide6/Qt.
- **Log Window** — accessible via View → Show Log Window; supports clear and
  save-to-file.

---

## Requirements

| Dependency | Minimum version |
|---|---|
| Python | 3.10+ |
| PySide6 | 6.5+ |
| pydicom | 2.4+ |
| pynetdicom | 2.0+ |

---

## Installation

### 1. Install Python

#### macOS

```bash
# Option A: Homebrew (recommended)
brew install python@3.12

# Option B: Download from https://www.python.org/downloads/macos/
```

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
git clone <repository-url> dicom_sync_gui
cd dicom_sync_gui
```

Or unzip the provided `dicom_sync_gui.zip` and navigate into the folder.

---

### 3. Create a virtual environment (recommended)

#### macOS / Linux

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

## Running the application

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

## Running tests

The project includes a comprehensive test suite (314 tests).

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

## Project structure

```
dicom_sync_gui/
├── main.py                  # Entry point, dark theme, dependency check
├── __init__.py              # Package version
├── __main__.py              # python -m support
├── requirements.txt         # pip dependencies
├── pytest.ini               # Test runner configuration
├── README.md
├── LICENSE
├── .gitignore
│
├── core/
│   ├── config.py            # AppConfig, PacsNode, load/save, export/import
│   ├── dicom_ops.py         # C-ECHO, C-FIND, C-MOVE operations
│   ├── storage_scp.py       # Built-in DICOM Storage SCP
│   └── transfer_engine.py   # Service loop, queue, stats, Qt signals
│
├── gui/
│   ├── main_window.py       # Main window, menus, engine wiring
│   ├── dashboard.py         # Dashboard: controls, queue table, stats
│   ├── settings_dialog.py   # PACS configuration dialog
│   ├── filter_groups_dialog.py  # Institution filter group editor
│   ├── unknown_institution_popup.py  # Alert popup for unknown institutions
│   ├── log_window.py        # Floating log viewer
│   └── styles.py            # Shared button stylesheet constants
│
└── tests/
    ├── conftest.py           # Shared fixtures
    ├── test_config.py
    ├── test_dicom_ops.py
    ├── test_transfer_engine.py
    ├── test_dashboard.py
    ├── test_settings_dialog.py
    ├── test_main_window.py
    ├── test_filter_groups_dialog.py
    └── test_filter_groups_export_import.py
```

---

## Configuration file location

The configuration is stored as JSON in a platform-specific directory:

| Platform | Path |
|---|---|
| macOS | `~/Library/Application Support/DicomSyncGUI/dicom_sync_config.json` |
| Linux | `~/.config/DicomSyncGUI/dicom_sync_config.json` |
| Windows | `%APPDATA%\DicomSyncGUI\dicom_sync_config.json` |

A log file (`dicom_sync_gui.log`) is written to the working directory.

---

## License

MIT — see [LICENSE](LICENSE) for details.
