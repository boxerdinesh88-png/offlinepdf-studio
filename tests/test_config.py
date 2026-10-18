"""Settings store, portable detection and data-dir isolation."""

from __future__ import annotations

from offlinepdf import APP_NAME
from offlinepdf.config import (
    Settings,
    data_dir,
    ensure_app_dirs,
    is_portable,
    version_banner,
)


def _isolate_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_data_dir_under_localappdata(monkeypatch, tmp_path):
    _isolate_data_dir(monkeypatch, tmp_path)
    import sys

    if sys.platform == "win32":
        assert data_dir() == tmp_path / "local" / APP_NAME
    else:  # pragma: no cover - platform branch
        assert data_dir().name == APP_NAME


def test_ensure_app_dirs_creates_structure(monkeypatch, tmp_path):
    _isolate_data_dir(monkeypatch, tmp_path)
    paths = ensure_app_dirs()
    for key in ("logs", "temp", "recovery", "cache"):
        assert paths[key].is_dir()
    # temp is app-owned
    assert paths["temp"] == data_dir() / "temp"


def test_settings_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path)
    assert s.get_str("theme", "light") == "light"
    s.set("theme", "dark")
    s.set("autosave_seconds", 10)
    s.save()

    again = Settings(path)
    assert again.get_str("theme") == "dark"
    assert again.get_int("autosave_seconds") == 10
    assert again.get_bool("thumbnails_visible") is True


def test_settings_corrupt_file_keeps_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    s = Settings(path)
    assert s.get_str("theme", "light") == "light"


def test_settings_typed_getters(tmp_path):
    s = Settings(tmp_path / "s.json")
    s.set("num", "not-a-number")
    s.set("flag", "yes")
    s.set("count", 42)
    assert s.get_int("num", 7) == 7
    assert s.get_str("count", "fallback") == "fallback"
    assert s.get_bool("flag") is True  # any non-empty string is truthy


def test_is_portable_matches_flag_presence():
    from offlinepdf.config import app_install_dir

    assert is_portable() == (app_install_dir() / "portable.flag").exists()


def test_version_banner():
    banner = version_banner()
    assert APP_NAME in banner and "py" in banner
