# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for OfflinePDF Studio.

One-folder build.  On Windows it is wrapped by Inno Setup; on macOS it
becomes a signed/notarizable .app bundle (and then a DMG).  Read-only
runtime assets (vendored fonts and OCR models) are bundled under
``_internal/assets`` (Windows) / ``Contents/Resources/assets`` (macOS) and
located via ``offlinepdf.config.resource_path`` at runtime.
"""

import glob
import os
import sys
import tomllib

APP_NAME = "OfflinePDF Studio"
PROJECT = SPECPATH  # directory containing this spec file
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

ICON_WIN = os.path.join(PROJECT, "assets", "app.ico")
ICON_MAC = os.path.join(PROJECT, "assets", "app.icns")
ICON = ICON_MAC if (IS_MAC and os.path.exists(ICON_MAC)) else ICON_WIN

ENTITLEMENTS = os.path.join(PROJECT, "packaging", "macos", "entitlements.plist")
if not os.path.exists(ENTITLEMENTS):
    ENTITLEMENTS = None

# --- version: pyproject.toml is the single source of truth ---------------
with open(os.path.join(PROJECT, "pyproject.toml"), "rb") as fh:
    VERSION = tomllib.load(fh)["project"]["version"]
_pkg_ns = {}
with open(os.path.join(PROJECT, "offlinepdf", "__init__.py"), encoding="utf-8") as fh:
    exec(fh.read(), _pkg_ns)
if _pkg_ns["__version__"] != VERSION:
    raise SystemExit(
        f"version mismatch: pyproject.toml={VERSION} "
        f"offlinepdf/__init__.py={_pkg_ns['__version__']}"
    )
_vt = tuple(int(p) for p in (VERSION.split(".") + ["0", "0", "0"])[:4])

datas = [
    (os.path.join(PROJECT, "assets", "ocr"), "assets/ocr"),
    (os.path.join(PROJECT, "assets", "fonts"), "assets/fonts"),
]

# RapidOCR ships YAML config files and default models inside its package.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

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

# --- OpenSSL: ship the libssl/libcrypto that match this Python's _ssl -----
# PyInstaller may pick an unrelated copy from PATH, which then fails to load.
try:
    import _ssl

    _ssl_dir = os.path.dirname(_ssl.__file__)
    if IS_WIN:
        _patterns = ("libssl-*.dll", "libcrypto-*.dll")
    elif IS_MAC:
        _patterns = ("libssl*.dylib", "libcrypto*.dylib")
    else:
        _patterns = ()
    _openssl = {
        os.path.basename(p).lower(): p
        for pattern in _patterns
        for p in glob.glob(os.path.join(_ssl_dir, pattern))
    }
    if _openssl:
        _binaries = [
            (dest, src, typ)
            for dest, src, typ in a.binaries
            if os.path.basename(dest).lower() not in _openssl
        ]
        _binaries += [(name, path, "BINARY") for name, path in _openssl.items()]
        a.binaries = _binaries
except Exception:
    pass

# Windows-only version resource.
version_info = None
if IS_WIN:
    from PyInstaller.utils.win32 import versioninfo as vi

    version_info = vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=_vt, prodvers=_vt),
        kids=[
            vi.StringFileInfo(
                [
                    vi.StringTable(
                        "040904B0",
                        [
                            vi.StringStruct("CompanyName", "Dinesh Kumar"),
                            vi.StringStruct("FileDescription", APP_NAME),
                            vi.StringStruct("FileVersion", VERSION),
                            vi.StringStruct("InternalName", "OfflinePDFStudio"),
                            vi.StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                            vi.StringStruct("ProductName", APP_NAME),
                            vi.StringStruct("ProductVersion", VERSION),
                        ],
                    )
                ]
            ),
            vi.VarFileInfo([vi.VarStruct("Translation", [0x0409, 1200])]),
        ],
    )

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    icon=ICON,
    version=version_info,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=IS_MAC,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=ENTITLEMENTS,
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

if IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=ICON,
        bundle_identifier="com.offlinepdf.studio",
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHumanReadableCopyright": "Copyright 2026 Dinesh Kumar. All rights reserved.",
            "NSHighResolutionCapable": True,
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "PDF Document",
                    "CFBundleTypeRole": "Editor",
                    "LSHandlerRank": "Alternate",
                    "LSItemContentTypes": ["com.adobe.pdf"],
                }
            ],
        },
        entitlements_file=ENTITLEMENTS,
    )
