"""Application paths, portable-mode detection and local settings.

All state lives in an app-owned folder.  No environment outside that folder
is ever written to, and nothing is ever sent anywhere.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from offlinepdf import APP_NAME, __version__

__all__ = [
    "APP_NAME",
    "Settings",
    "app_install_dir",
    "data_dir",
    "ensure_app_dirs",
    "is_frozen",
    "is_linux",
    "is_macos",
    "is_portable",
    "is_windows",
    "resource_path",
    "temp_dir",
]

_DEFAULT_SETTINGS: dict[str, Any] = {
    "theme": "light",
    "last_dir": "",
    "thumbnails_visible": True,
    "autosave_seconds": 30,
    "recent_files": [],
    "image_max_px": 3000,
    "image_jpeg_quality": 90,
    "image_png_lossless": True,
    "ocr_auto": True,
    "ocr_language": "en",
    "ocr_dpi": 300,
    "ocr_review_threshold": 0.70,
    "ocr_searchable_layer": True,
    "ocr_deskew": True,
    "ocr_denoise": True,
    "ocr_contrast": True,
    "ocr_binarize": False,
}


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def app_install_dir() -> Path:
    """Directory containing the executable (frozen) or the project root."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # offlinepdf/config.py -> offlinepdf/ -> project root
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """Path to a bundled read-only resource.

    Frozen builds unpack data next to the executable (one-folder mode) under
    ``sys._MEIPASS``; from source the project root is used.  Never write here -
    all writable state belongs in :func:`data_dir`.
    """
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = Path(__file__).resolve().parent.parent
    return base.joinpath(*parts)


def is_portable() -> bool:
    """Portable mode is enabled by a ``portable.flag`` next to the app."""
    return (app_install_dir() / "portable.flag").exists()


def data_dir() -> Path:
    """App-owned state folder (settings, logs, temp, recovery)."""
    if is_portable():
        base = app_install_dir() / "portable_data"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        base = base / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        base = base / APP_NAME
    return base


def ensure_app_dirs() -> dict[str, Path]:
    """Create and return the standard subfolders of :func:`data_dir`."""
    root = data_dir()
    paths = {
        "root": root,
        "logs": root / "logs",
        "temp": root / "temp",
        "recovery": root / "recovery",
        "cache": root / "cache",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def temp_dir() -> Path:
    """App-owned temp folder; contents are wiped on every clean exit."""
    return ensure_app_dirs()["temp"]


class Settings:
    """Tiny JSON settings store with atomic writes."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (ensure_app_dirs()["root"] / "settings.json")
        self._data: dict[str, Any] = dict(_DEFAULT_SETTINGS)
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        """Read settings from disk, keeping defaults for anything missing."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            for key, value in raw.items():
                self._data[key] = value

    def save(self) -> None:
        """Atomically persist settings."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get_bool(self, key: str, default: bool = False) -> bool:
        return bool(self._data.get(key, default))

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self._data.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_str(self, key: str, default: str = "") -> str:
        value = self._data.get(key, default)
        return value if isinstance(value, str) else default


def version_banner() -> str:
    """Version banner used by logs and the selftest."""
    frozen = "frozen" if is_frozen() else "source"
    portable = "portable" if is_portable() else "installed"
    return f"{APP_NAME} {__version__} ({frozen}, {portable}, py{sys.version.split()[0]})"

