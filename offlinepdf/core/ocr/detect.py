"""Per-page detection: real text layer vs. text that exists only as pixels.

A page is *scanned* (image-based) when it has no extractable text but does
paint something (images or vector outlines), or when large embedded images
dwarf the little real text it has.  Pages already carrying our OCR layer are
reported as ``ocr`` so they are never recognised twice.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf

__all__ = ["OCR_MARK", "PageKind", "classify_page", "scan_dpi"]

OCR_MARK = b"/OPDFOcr"

#: image cover (fraction of the page) from which a page counts as "image heavy"
_IMAGE_HEAVY = 0.60
#: real text covering less than this fraction of the page is incidental
#: (a stamped page number or header on a scan), more means a designed page
#: such as a slide with a full-bleed background picture
_INCIDENTAL_TEXT = 0.004
_MIN_DRAWINGS = 25  # vector-outlined text paints many paths


@dataclass(frozen=True)
class PageKind:
    """Classification of one page."""

    kind: str  # "text" | "scanned" | "ocr" | "blank"
    text_ratio: float  # extractable text area / page area
    image_ratio: float  # embedded image area / page area (clipped, capped at 1)

    @property
    def needs_ocr(self) -> bool:
        return self.kind == "scanned"


def _content_has_mark(page: pymupdf.Page) -> bool:
    doc = page.parent
    try:
        return any(OCR_MARK in doc.xref_stream(xref) for xref in page.get_contents())
    except Exception:
        return False


def scan_dpi(page: pymupdf.Page) -> float:
    """Effective resolution of the largest image on the page (0 if none).

    Recognition degrades below ~150 DPI; the UI warns about such scans.
    """
    best_area, dpi = 0.0, 0.0
    for info in page.get_image_info():
        rect = pymupdf.Rect(info["bbox"])
        if rect.is_empty or info.get("width", 0) <= 0:
            continue
        area = rect.width * rect.height
        if area > best_area:
            longest_pt = max(rect.width, rect.height)
            longest_px = max(info["width"], info["height"])
            best_area, dpi = area, 72.0 * longest_px / longest_pt
    return dpi


def classify_page(page: pymupdf.Page) -> PageKind:
    """Classify ``page`` (a PyMuPDF page; cheap - no rendering)."""
    area = max(page.rect.width * page.rect.height, 1.0)
    if _content_has_mark(page):
        return PageKind("ocr", 0.0, 0.0)

    text_area = 0.0
    for x0, y0, x1, y1, word, *_rest in page.get_text("words"):
        if word.strip():
            text_area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    text_ratio = min(1.0, text_area / area)

    image_area = 0.0
    for info in page.get_image_info():
        rect = pymupdf.Rect(info["bbox"]) & page.rect
        if not rect.is_empty:
            image_area += rect.width * rect.height
    image_ratio = min(1.0, image_area / area)

    if text_ratio == 0.0:
        if image_ratio > 0.02:
            return PageKind("scanned", text_ratio, image_ratio)
        try:
            outlined = len(page.get_drawings()) >= _MIN_DRAWINGS
        except Exception:
            outlined = False
        kind = "scanned" if outlined else "blank"
        return PageKind(kind, text_ratio, image_ratio)
    if image_ratio >= _IMAGE_HEAVY and text_ratio < _INCIDENTAL_TEXT:
        return PageKind("scanned", text_ratio, image_ratio)
    return PageKind("text", text_ratio, image_ratio)
