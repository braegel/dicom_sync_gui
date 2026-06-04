# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for DICOM Sync GUI — macOS .app bundle."""

import re
import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)


def _read_version() -> str:
    """Read ``__version__`` from ``__init__.py`` so the spec and the
    Python package share one source of truth and can't drift."""
    init_path = ROOT / "__init__.py"
    text = init_path.read_text(encoding="utf-8")
    # Anchor at start of line AND require nothing after the closing
    # quote except whitespace/end-of-line so a stray
    # ``__version_history__`` line can't ever be mistaken for the
    # version literal.
    m = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$',
        text, re.MULTILINE)
    if not m:
        raise RuntimeError(
            f"could not find __version__ in {init_path}")
    return m.group(1)


_VERSION = _read_version()

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "pydicom.encoders.gdcm",
        "pydicom.encoders.pylibjpeg",
        "pydicom.encoders.native",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pytest"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DICOM Sync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DICOM Sync",
)

app = BUNDLE(
    coll,
    name="DICOM Sync.app",
    icon=str(ROOT / "assets" / "AppIcon.icns"),
    bundle_identifier="com.dicomsync.gui",
    info_plist={
        "CFBundleDisplayName": "DICOM Sync",
        "CFBundleShortVersionString": _VERSION,
        "CFBundleVersion": _VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSLocalNetworkUsageDescription":
            "DICOM Sync needs local network access to communicate "
            "with PACS servers.",
    },
)
