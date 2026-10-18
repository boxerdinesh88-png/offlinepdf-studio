# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for OfflinePDF Studio.

One-folder, windowed (no console) build.  Read-only runtime assets (vendored
fonts and OCR models) are bundled under ``_internal/assets`` and located via
``offlinepdf.config.resource_path`` at runtime.  Build/dist/work output paths
are supplied on the command line by ``build_installer.bat``.
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP_NAME = "OfflinePDF Studio"
PROJECT = SPECPATH  # directory containing this spec file

datas = [
    (os.path.join(PROJECT, "assets", "ocr"), "assets/ocr"),
    (os.path.join(PROJECT, "assets", "fonts"), "assets/fonts"),
]

# RapidOCR ships YAML config files and default models inside its package.
datas += collect_data_files("rapidocr_onnxruntime")

hiddenimports = [
    "rapidocr_onnxruntime",
    "shapely",
    "pyclipper",
    "uharfbuzz",
]
hiddenimports += collect_submodules("rapidocr_onnxruntime")

a = Analysis(
    [os.path.join(PROJECT, "main.py")],
    pathex=[PROJECT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "pytest_asyncio"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OfflinePDFStudio",
)
