"""Offline OCR (M6): language packs + RapidOCR engine.

Public surface of the OCR subsystem.  Importing this module stays light; the
heavy inference stack is loaded lazily on first recognition.
"""

from __future__ import annotations

from .engine import OcrEngine, OcrError, OcrWord, ocr_page
from .manifest import (
    LanguagePack,
    available_languages,
    default_language,
    ocr_assets_dir,
    require_language,
    verify_assets,
)

__all__ = [
    "LanguagePack",
    "OcrEngine",
    "OcrError",
    "OcrWord",
    "available_languages",
    "default_language",
    "ocr_assets_dir",
    "ocr_page",
    "require_language",
    "verify_assets",
]
