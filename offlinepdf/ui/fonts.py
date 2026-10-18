"""Make PDF and bundled fonts available to Qt for WYSIWYG editing.

The inline editor must draw typed text with the same face the saved PDF
will use.  Qt only knows installed system fonts, so:

* :func:`register_bundled_fonts` adds ``assets/fonts`` (the faces the edit
  engine falls back to) once per process;
* :func:`pdf_font_family` loads an embedded font program straight from the
  PDF (subset or not) and returns the family name Qt registered it under.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QFontDatabase

__all__ = ["pdf_font_family", "register_bundled_fonts"]

logger = logging.getLogger(__name__)

_registered = False
_pdf_families: dict[str, str | None] = {}


def register_bundled_fonts() -> int:
    """Register every bundled TTF/OTF with Qt (idempotent); returns the count."""
    global _registered
    if _registered:
        return 0
    _registered = True
    from offlinepdf.config import resource_path

    root = resource_path("assets", "fonts")
    count = 0
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in (".ttf", ".otf"):
            continue
        if QFontDatabase.addApplicationFont(str(path)) >= 0:
            count += 1
    logger.info("registered %d bundled fonts with Qt", count)
    return count


def pdf_font_family(key: str, program: bytes | None) -> str | None:
    """Qt family for an embedded font program (cached per font object ``key``)."""
    if key in _pdf_families:
        return _pdf_families[key]
    family = None
    if program:
        font_id = QFontDatabase.addApplicationFontFromData(QByteArray(program))
        if font_id >= 0:
            families = QFontDatabase.applicationFontFamilies(font_id)
            family = families[0] if families else None
    _pdf_families[key] = family
    return family
