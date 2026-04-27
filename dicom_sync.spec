# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for DICOM Sync GUI — macOS .app bundle."""

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)

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
        "CFBundleShortVersionString": "1.0.7",
        "CFBundleVersion": "1.0.7",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSLocalNetworkUsageDescription":
            "DICOM Sync needs local network access to communicate "
            "with PACS servers.",
    },
)
