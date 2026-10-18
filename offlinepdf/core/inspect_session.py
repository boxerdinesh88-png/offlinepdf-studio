"""Cached inspect-mode state for one open document.

Everything the Inspect UI needs, computed lazily and remembered:

* page models      (content parse + XY-cut,  ``model()``)
* style profiles   (detection pipeline,     ``profile()``)
* font programs    (one shared cache for the whole document)
* pixel renders    (only if a page actually needs the colour self-check)
* link rects       (converted to model space for the link decoration)
* glyph coverage   (per page + document aggregate)
* performance numbers (ms per page for model/detect/render)

``warm_next()`` builds one cold page per call so a QTimer chain can spread
the work across event-loop ticks without freezing the UI.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pymupdf

from offlinepdf.core.document import PdfSession
from offlinepdf.core.font_program import FontPrograms
from offlinepdf.core.glyph_coverage import (
    DocumentCoverage,
    PageCoverage,
    aggregate_coverage,
    page_coverage,
)
from offlinepdf.core.style_detector import Preset, StyleProfile, detect_style
from offlinepdf.core.text_model import HitResult, PageModel

PixelSampler = Callable[[float, float, float, float], list[tuple[int, int, int]]]


def _read_region(
    render: pymupdf.Pixmap, x0: float, y0: float, x1: float, y1: float
) -> list[tuple[int, int, int]]:
    """Pull RGB pixels of a view-space rect out of a cached render."""
    width = render.width
    height = render.height
    channels = min(3, render.n)
    stride = render.stride
    px0 = max(0, min(width - 1, int(x0)))
    py0 = max(0, min(height - 1, int(y0)))
    px1 = max(px0 + 1, min(width, int(x1 + 0.999)))
    py1 = max(py0 + 1, min(height, int(y1 + 0.999)))
    samples = render.samples
    out: list[tuple[int, int, int]] = []
    for py in range(py0, py1):
        row = py * stride
        for px in range(px0, px1):
            base = row + px * channels
            if base + 2 >= len(samples):
                break
            out.append((samples[base], samples[base + 1], samples[base + 2]))
    return out


def _page_size(session: PdfSession, index: int) -> tuple[float, float]:
    """Unrotated page size (mediabox) used only for the failed-model fallback."""
    box = session.pdf.pages[index].mediabox
    return float(box.width), float(box.height)


class InspectSession:
    """All cached inspect state for one document."""

    def __init__(self, document: PdfSession) -> None:
        self._document = document
        self._models: dict[int, PageModel] = {}
        self._profiles: dict[int, StyleProfile] = {}
        self._coverage: dict[int, PageCoverage] = {}
        self._doc_coverage: DocumentCoverage | None = None
        self._programs = FontPrograms()
        self._renders: dict[int, pymupdf.Pixmap] = {}
        self._links: dict[int, list[tuple[float, float, float, float]]] = {}
        self._model_ms: dict[int, float] = {}
        self._detect_ms: dict[int, float] = {}
        self._render_ms: dict[int, float] = {}
        self._coverage_ms: dict[int, float] = {}

    # ------------------------------------------------------------------ #
    # basics
    # ------------------------------------------------------------------ #
    @property
    def document(self) -> PdfSession:
        return self._document

    @property
    def page_count(self) -> int:
        return self._document.page_count

    @property
    def programs(self) -> FontPrograms:
        return self._programs

    # ------------------------------------------------------------------ #
    # models / profiles
    # ------------------------------------------------------------------ #
    def _failed_model(self, index: int, message: str) -> PageModel:
        try:
            width, height = _page_size(self._document, index)
        except Exception:
            width, height = (612.0, 792.0)
        return PageModel(
            page_index=index,
            rotation=0,
            mediabox=(0.0, 0.0, float(width), float(height)),
            size_unrot=(float(width), float(height)),
            size_view=(float(width), float(height)),
            rotation_matrix=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
            warnings=[message],
        )

    def model(self, index: int) -> PageModel:
        cached = self._models.get(index)
        if cached is not None:
            return cached
        started = time.perf_counter()
        try:
            pdf = self._document.pdf
            fitz_doc = self._document.mupdf
            model = PageModel.build(pdf, fitz_doc, index)
            self._programs.add_fonts(model.fonts, model.font_dicts)
        except Exception as exc:
            model = self._failed_model(index, f"page model failed: {exc}")
        self._model_ms[index] = (time.perf_counter() - started) * 1000.0
        self._models[index] = model
        return model

    def _sampler(self, index: int) -> PixelSampler:
        def sample(x0: float, y0: float, x1: float, y1: float) -> list[tuple[int, int, int]]:
            render = self._renders.get(index)
            if render is None:
                started = time.perf_counter()
                page = self._document.mupdf.load_page(index)
                render = page.get_pixmap(alpha=False)
                self._render_ms[index] = (time.perf_counter() - started) * 1000.0
                self._renders[index] = render
            return _read_region(render, x0, y0, x1, y1)

        return sample

    def _links_model(self, index: int) -> list[tuple[float, float, float, float]]:
        cached = self._links.get(index)
        if cached is not None:
            return cached
        out: list[tuple[float, float, float, float]] = []
        try:
            model = self.model(index)
            fitz_page = self._document.mupdf[index]
            for link in fitz_page.get_links():
                rect = link.get("from")
                if rect is None:
                    continue
                ax, ay = model.from_view(rect.x0, rect.y0)
                bx, by = model.from_view(rect.x1, rect.y1)
                out.append((min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)))
        except Exception:
            out = []
        self._links[index] = out
        return out

    def profile(self, index: int) -> StyleProfile:
        cached = self._profiles.get(index)
        if cached is not None:
            return cached
        model = self.model(index)
        started = time.perf_counter()
        profile = detect_style(
            model,
            self._programs,
            links=self._links_model(index),
            sampler=self._sampler(index),
        )
        self._detect_ms[index] = (time.perf_counter() - started) * 1000.0
        self._profiles[index] = profile
        return profile

    def presets(self, index: int) -> list[Preset]:
        return list(self.profile(index).presets)

    def hit_test(self, index: int, x: float, y: float) -> HitResult | None:
        return self.model(index).hit_test(x, y)

    def debug_geometry(self, index: int) -> dict[str, Any]:
        return self.model(index).debug_geometry()

    # ------------------------------------------------------------------ #
    # coverage
    # ------------------------------------------------------------------ #
    def coverage(self, index: int) -> PageCoverage:
        cached = self._coverage.get(index)
        if cached is not None:
            return cached
        model = self.model(index)
        started = time.perf_counter()
        cov = page_coverage(model, self._programs)
        self._coverage_ms[index] = (time.perf_counter() - started) * 1000.0
        self._coverage[index] = cov
        return cov

    def document_coverage(self) -> DocumentCoverage:
        """Coverage over every page (warms remaining pages as needed)."""
        if self._doc_coverage is not None:
            return self._doc_coverage
        pages = [self.coverage(index) for index in range(self.page_count)]
        self._doc_coverage = aggregate_coverage(pages)
        return self._doc_coverage

    # ------------------------------------------------------------------ #
    # progressive warming
    # ------------------------------------------------------------------ #
    def warm_progress(self) -> tuple[int, int]:
        done = sum(1 for i in range(self.page_count) if i in self._profiles)
        return (done, self.page_count)

    def warm_next(self) -> bool:
        """Build model + profile for the next cold page.

        Returns True while more cold pages remain after this call.
        """
        total = self.page_count
        for index in range(total):
            if index not in self._profiles:
                self.profile(index)
                return index + 1 < total
        return False

    # ------------------------------------------------------------------ #
    # invalidation / perf
    # ------------------------------------------------------------------ #
    def invalidate(self) -> None:
        """Drop everything (after the document was modified)."""
        self._models.clear()
        self._profiles.clear()
        self._coverage.clear()
        self._doc_coverage = None
        self._renders.clear()
        self._links.clear()
        self._model_ms.clear()
        self._detect_ms.clear()
        self._render_ms.clear()
        self._coverage_ms.clear()
        self._programs = FontPrograms()

    def perf_stats(self) -> dict[str, Any]:
        def total(values: dict[int, float]) -> float:
            return round(sum(values.values()), 2)

        per_page = []
        for index in range(self.page_count):
            if index in self._model_ms or index in self._detect_ms:
                per_page.append(
                    {
                        "page": index,
                        "model_ms": round(self._model_ms.get(index, 0.0), 2),
                        "detect_ms": round(self._detect_ms.get(index, 0.0), 2),
                        "render_ms": round(self._render_ms.get(index, 0.0), 2),
                        "coverage_ms": round(self._coverage_ms.get(index, 0.0), 2),
                    }
                )
        warmed = len(per_page)
        model_total = total(self._model_ms)
        detect_total = total(self._detect_ms)
        return {
            "page_count": self.page_count,
            "warmed_pages": warmed,
            "totals": {
                "model_ms": model_total,
                "detect_ms": detect_total,
                "render_ms": total(self._render_ms),
                "coverage_ms": total(self._coverage_ms),
            },
            "per_page": per_page,
            "pages_per_second": (
                round(warmed / ((model_total + detect_total) / 1000.0), 2)
                if (model_total + detect_total) > 0
                else 0.0
            ),
        }
