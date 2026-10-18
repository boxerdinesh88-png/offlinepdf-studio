"""Shared fixtures.

The network guard is installed at import time so every test runs with
networking blocked, and Qt is forced to the offscreen platform before any
Qt import happens.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from offlinepdf import netguard  # noqa: E402

netguard.install()

CORPUS_DIR = ROOT / "tests" / "_corpus"
_GENERATOR = ROOT / "scripts" / "make_corpus.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_corpus", _GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stale(pdf: Path) -> bool:
    return not pdf.exists() or pdf.stat().st_mtime < _GENERATOR.stat().st_mtime


@pytest.fixture(scope="session")
def corpus_dir() -> Path:
    """Small corpus (basic.pdf), rebuilt when the generator changes."""
    module = _load_generator()
    if _stale(CORPUS_DIR / "basic.pdf"):
        module.build(CORPUS_DIR, ["basic"])
    return CORPUS_DIR


@pytest.fixture(scope="session")
def perf_corpus() -> Path:
    """500-page / ~50 MB performance document (cached on disk)."""
    module = _load_generator()
    if _stale(CORPUS_DIR / "perf.pdf"):
        module.build(CORPUS_DIR, ["perf"])
    return CORPUS_DIR


_EDIT_CASES = [
    "basic",
    "type3",
    "outlined",
    "cid",
    "missing_glyph",
    "devanagari",
    "rtl",
    "encrypted",
    "signed",
]


@pytest.fixture(scope="session")
def edit_corpus() -> Path:
    """M3 round-trip corpus (every case except the perf document)."""
    module = _load_generator()
    missing = [case for case in _EDIT_CASES if _stale(CORPUS_DIR / f"{case}.pdf")]
    if missing:
        module.build(CORPUS_DIR, missing)
    return CORPUS_DIR


@pytest.fixture(scope="session")
def qapp():
    """Session-wide offscreen QApplication."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
