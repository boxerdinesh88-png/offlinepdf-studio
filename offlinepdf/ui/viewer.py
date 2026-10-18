"""Virtualized page viewer.

Pages are laid out in one long canvas; only pages intersecting the viewport
are rendered and kept in an LRU cache with a byte budget, so a 500-page
document costs the same as a 5-page one while scrolling.

Coordinates: the canvas works in *display space* (PyMuPDF's ``page.rect``,
which already includes /Rotate), so hit-testing later is a direct match
against the pixmap the user sees.

Optional :class:`CanvasDelegate` s (image tool, OCR review overlay) paint
extra layers below/above the page content and get first refusal on mouse
events, in the order they were added.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import pymupdf
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QScrollArea, QWidget

from offlinepdf.core.document import PdfSession
from offlinepdf.ui import theme

__all__ = ["CanvasDelegate", "PageCanvas", "PixmapCache", "Viewer"]

logger = logging.getLogger(__name__)

GAP_PT = 14.0  # vertical gap between pages, in points
MARGIN_PT = 16.0  # outer margin, in points
MIN_ZOOM = 0.1
MAX_ZOOM = 6.0
_CACHE_BUDGET = 128 * 1024 * 1024  # 128 MB of decoded pixmaps

Rect4 = tuple[float, float, float, float]
Hit = tuple[int, float, float]  # (page index, view x, view y)


def _as_rect(value: Any) -> Rect4:
    x0, y0, x1, y1 = value
    return (float(x0), float(y0), float(x1), float(y1))


@dataclass
class PageGeom:
    """Display-space geometry of one page at the current zoom."""

    index: int
    x: float
    y: float
    width: float
    height: float

    @property
    def bottom(self) -> float:
        return self.y + self.height


class CanvasDelegate(Protocol):
    """Hooks for tools that draw on and interact with the canvas.

    ``paint_layer`` is called with ``"below"`` (under the page content; only
    when ``page_needs_alpha``), ``"above"`` (over it, clipped to the page) and
    ``"chrome"`` (handles and guides, unclipped).  Mouse hooks return True
    when they consumed the event.
    """

    def page_needs_alpha(self, index: int) -> bool: ...

    def paint_layer(self, painter: QPainter, geom: PageGeom, layer: str) -> None: ...

    def mouse_press(self, event: QMouseEvent) -> bool: ...

    def mouse_move(self, event: QMouseEvent) -> bool: ...

    def mouse_release(self, event: QMouseEvent) -> bool: ...

    def mouse_double_click(self, event: QMouseEvent) -> bool: ...


class PixmapCache:
    """LRU cache of rendered page pixmaps bounded by decoded byte size."""

    def __init__(self, budget: int = _CACHE_BUDGET) -> None:
        self._budget = budget
        self._items: dict[tuple[int, int, bool], QPixmap] = {}
        self._sizes: dict[tuple[int, int, bool], int] = {}
        self._used = 0

    @staticmethod
    def _scale_key(scale: float) -> int:
        return round(scale * 1000)

    def get(self, page: int, scale: float, alpha: bool = False) -> QPixmap | None:
        key = (page, self._scale_key(scale), alpha)
        pix = self._items.get(key)
        if pix is not None:
            # refresh LRU order
            self._items[key] = self._items.pop(key)
        return pix

    def put(self, page: int, scale: float, pix: QPixmap, alpha: bool = False) -> None:
        key = (page, self._scale_key(scale), alpha)
        old = self._items.pop(key, None)
        if old is not None:
            self._used -= self._sizes.pop(key, 0)
        self._items[key] = pix
        size = pix.width() * pix.height() * 4
        self._sizes[key] = size
        self._used += size
        self._evict()

    def _evict(self) -> None:
        while self._used > self._budget and len(self._items) > 1:
            key = next(iter(self._items))
            self._items.pop(key)
            self._used -= self._sizes.pop(key, 0)

    def clear(self) -> None:
        self._items.clear()
        self._sizes.clear()
        self._used = 0

    @property
    def used_bytes(self) -> int:
        return self._used


class PageCanvas(QWidget):
    """Scrolling canvas that paints only the visible pages.

    It also owns the *overlay* layer used by Inspect/Edit mode: hover and
    selection rectangles, the debug geometry (blocks, spans, baselines) and
    the inline editor host.  All overlay rectangles are in view space (the
    page's displayed coordinates at zoom 1, i.e. PyMuPDF ``page.rect``).
    """

    hoverMoved = Signal(object)  # tuple[int, float, float] | None
    spanClicked = Signal(object, bool)  # hit tuple | None, double-click flag

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session: PdfSession | None = None
        self._zoom = 1.0
        self._geoms: list[PageGeom] = []
        self._content_width = 0.0
        self._cache = PixmapCache()
        self._dpr = 1.0
        self._hover: tuple[int, Rect4] | None = None
        self._selection: tuple[int, Rect4] | None = None
        self._debug: tuple[int, dict[str, Any]] | None = None
        self._delegates: list[CanvasDelegate] = []
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)

    # --- document ---------------------------------------------------------

    def set_session(self, session: PdfSession | None) -> None:
        """Attach a document (or clear with None) and recompute layout."""
        self._session = session
        self._cache.clear()
        self.clear_overlays()
        self._recompute_geometry()
        self.update()

    def session(self) -> PdfSession | None:
        return self._session

    def add_delegate(self, delegate: CanvasDelegate) -> None:
        """Install a tool that paints extra layers and may pre-empt the mouse."""
        if delegate not in self._delegates:
            self._delegates.append(delegate)
        self.update()

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float) -> None:
        """Set zoom factor (clamped) keeping the top-left visible anchor."""
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        if abs(zoom - self._zoom) < 1e-6:
            return
        self._zoom = zoom
        self._recompute_geometry()
        self.update()

    def page_count(self) -> int:
        return len(self._geoms)

    def refresh_document(self) -> None:
        """Drop cached pixmaps and repaint the current document state."""
        self._cache.clear()
        self._recompute_geometry()
        self.update()

    # --- overlays ---------------------------------------------------------

    def set_hover(self, hit: Hit | None, rect: Rect4 | None = None) -> None:
        """Highlight the hovered span (``rect`` in view space, page ``hit[0]``)."""
        self._hover = (hit[0], rect) if hit is not None and rect is not None else None
        self.update()

    def set_selection(self, page: int | None, rect: Rect4 | None) -> None:
        self._selection = (page, rect) if page is not None and rect is not None else None
        self.update()

    def set_debug_geometry(self, page: int | None, geometry: dict[str, Any] | None) -> None:
        """Draw block/span/char rectangles and baselines for one page."""
        self._debug = (page, geometry) if page is not None and geometry else None
        self.update()

    def clear_overlays(self) -> None:
        self._hover = None
        self._selection = None
        self._debug = None
        self.update()

    def locate(self, canvas_x: float, canvas_y: float) -> Hit | None:
        """Map a canvas position to ``(page, view x, view y)``."""
        for geom in self._geoms:
            if geom.x <= canvas_x <= geom.x + geom.width and geom.y <= canvas_y <= geom.bottom:
                zoom = self._zoom or 1.0
                return (geom.index, (canvas_x - geom.x) / zoom, (canvas_y - geom.y) / zoom)
        return None

    def _to_canvas(self, geom: PageGeom, rect: Rect4) -> QRectF:
        zoom = self._zoom
        return QRectF(
            geom.x + rect[0] * zoom,
            geom.y + rect[1] * zoom,
            (rect[2] - rect[0]) * zoom,
            (rect[3] - rect[1]) * zoom,
        )

    # --- geometry ---------------------------------------------------------

    def _recompute_geometry(self) -> None:
        """Rebuild page rectangles from the document at the current zoom."""
        self._geoms.clear()
        if self._session is None or self._session.is_closed:
            self._content_width = 0.0
            self.resize(0, 0)
            return

        doc = self._session.mupdf
        margin = MARGIN_PT * self._zoom
        gap = GAP_PT * self._zoom
        max_w = 0.0
        raw: list[tuple[int, float, float]] = []
        for i in range(doc.page_count):
            rect = doc.load_page(i).rect
            raw.append((i, rect.width, rect.height))
            max_w = max(max_w, rect.width)
        content_w = max_w * self._zoom
        self._content_width = content_w
        y = margin
        for index, w, h in raw:
            pw, ph = w * self._zoom, h * self._zoom
            x = margin + (content_w - pw) / 2.0
            self._geoms.append(PageGeom(index=index, x=x, y=y, width=pw, height=ph))
            y += ph + gap
        total_h = y - gap + margin if self._geoms else 0.0
        total_w = content_w + 2 * margin
        self.resize(int(total_w), int(total_h))

    def page_at_y(self, y: float) -> int:
        """Page index whose band contains ``y`` (clamped to the first page)."""
        if not self._geoms:
            return 0
        for geom in self._geoms:
            if y <= geom.bottom:
                return geom.index
        return self._geoms[-1].index

    def page_top(self, index: int) -> int:
        """Canvas y of the top of ``index``."""
        if 0 <= index < len(self._geoms):
            return int(self._geoms[index].y)
        return 0

    def geom(self, index: int) -> PageGeom | None:
        if 0 <= index < len(self._geoms):
            return self._geoms[index]
        return None

    def max_page_width_pt(self) -> float:
        return max((g.width for g in self._geoms), default=0.0) / self._zoom

    # --- rendering --------------------------------------------------------

    def _render_page(self, geom: PageGeom, alpha: bool = False) -> QPixmap | None:
        """Render a page; ``alpha`` keeps the paper transparent (MuPDF premultiplies)."""
        session = self._session
        if session is None or session.is_closed:
            return None
        scale = self._zoom * self._dpr
        cached = self._cache.get(geom.index, scale, alpha)
        if cached is not None:
            return cached
        try:
            page = session.mupdf.load_page(geom.index)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=alpha)
        except Exception:
            logger.exception("render failed for page %d", geom.index + 1)
            return None
        fmt = (
            QImage.Format.Format_RGBA8888_Premultiplied if alpha else QImage.Format.Format_RGB888
        )
        image = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)
        qpixmap = QPixmap.fromImage(image.copy())
        self._cache.put(geom.index, scale, qpixmap, alpha)
        return qpixmap

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            look = theme.current()
            painter.fillRect(event.rect(), QColor(look.canvas))
            if not self._geoms:
                return
            top = event.rect().top()
            bottom = event.rect().bottom()
            for geom in self._geoms:
                if geom.y > bottom:
                    break
                if geom.bottom < top:
                    continue
                alpha = any(d.page_needs_alpha(geom.index) for d in self._delegates)
                pixmap = self._render_page(geom, alpha)
                target = QRect(
                    int(geom.x),
                    int(geom.y),
                    int(geom.width),
                    int(geom.height),
                )
                self._paint_shadow(painter, target, look.name == "dark")
                if pixmap is not None and not pixmap.isNull():
                    if alpha:
                        painter.fillRect(target, QColor(255, 255, 255))
                        self._paint_delegate(painter, geom, "below")
                    painter.drawPixmap(target, pixmap)
                else:
                    painter.fillRect(target, QColor(210, 210, 210))
                self._paint_delegate(painter, geom, "above")
                self._paint_overlays(painter, geom)
                self._paint_delegate(painter, geom, "chrome")
        finally:
            painter.end()

    @staticmethod
    def _paint_shadow(painter: QPainter, target: QRect, dark: bool) -> None:
        """Soft drop shadow (and a hairline edge in dark mode) behind a page."""
        base = 70 if dark else 28
        for step, alpha in ((4, base // 4), (3, base // 3), (2, base // 2), (1, base)):
            painter.fillRect(
                target.adjusted(-step + 1, -step + 2, step - 1, step + 1), QColor(0, 0, 0, alpha)
            )
        if dark:
            painter.setPen(QPen(QColor(255, 255, 255, 28), 1))
            painter.drawRect(target.adjusted(-1, -1, 0, 0))

    def _paint_delegate(self, painter: QPainter, geom: PageGeom, layer: str) -> None:
        for delegate in self._delegates:
            painter.save()
            try:
                if layer != "chrome":
                    painter.setClipRect(QRectF(geom.x, geom.y, geom.width, geom.height))
                delegate.paint_layer(painter, geom, layer)
            except Exception:  # a tool bug must never blank the page
                logger.exception("canvas delegate failed to paint %s", layer)
            finally:
                painter.restore()

    def _paint_overlays(self, painter: QPainter, geom: PageGeom) -> None:
        painter.save()
        painter.setClipRect(
            QRectF(geom.x, geom.y, geom.width, geom.height).adjusted(-2, -2, 2, 2)
        )
        if self._debug is not None and self._debug[0] == geom.index:
            geometry = self._debug[1]
            painter.setPen(QPen(QColor(70, 130, 220, 180), 1))
            for rect in geometry.get("blocks", []):
                painter.drawRect(self._to_canvas(geom, _as_rect(rect)))
            painter.setPen(QPen(QColor(40, 170, 90, 180), 1))
            for rect in geometry.get("spans", []):
                painter.drawRect(self._to_canvas(geom, _as_rect(rect)))
            painter.setPen(QPen(QColor(200, 90, 40, 200), 1))
            for pair in geometry.get("baselines", []):
                first, last = pair
                painter.drawLine(
                    QPointF(geom.x + first[0] * self._zoom, geom.y + first[1] * self._zoom),
                    QPointF(geom.x + last[0] * self._zoom, geom.y + last[1] * self._zoom),
                )
        if self._hover is not None and self._hover[0] == geom.index:
            painter.fillRect(
                self._to_canvas(geom, self._hover[1]), QColor(60, 140, 255, 55)
            )
            painter.setPen(QPen(QColor(30, 110, 230, 220), 1.5))
            painter.drawRect(self._to_canvas(geom, self._hover[1]).adjusted(-1, -1, 1, 1))
        if self._selection is not None and self._selection[0] == geom.index:
            painter.setPen(QPen(QColor(230, 140, 20, 230), 1.5))
            painter.drawRect(self._to_canvas(geom, self._selection[1]).adjusted(-1, -1, 1, 1))
        painter.restore()

    def wheel_zoom(self, delta: int, anchor_y: int | None = None) -> None:
        """Ctrl+wheel zoom around a canvas y anchor."""
        factor = 1.1 if delta > 0 else 1 / 1.1
        self.set_zoom(self._zoom * factor)

    # --- mouse ------------------------------------------------------------

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if any(d.mouse_move(event) for d in self._delegates):
            return
        pos = event.position()
        self.hoverMoved.emit(self.locate(pos.x(), pos.y()))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if any(d.mouse_press(event) for d in self._delegates):
            return
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            self.spanClicked.emit(self.locate(pos.x(), pos.y()), False)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if any(d.mouse_release(event) for d in self._delegates):
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if any(d.mouse_double_click(event) for d in self._delegates):
            return
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            self.spanClicked.emit(self.locate(pos.x(), pos.y()), True)
        super().mouseDoubleClickEvent(event)


class Viewer(QScrollArea):
    """Scrollable viewer exposing zoom, navigation and document state."""

    zoomChanged = Signal(float)
    visiblePageChanged = Signal(int)
    documentClosed = Signal()
    renderStats = Signal(int, float)  # page, last render ms (diagnostics)
    hoverMoved = Signal(object)  # tuple[int, float, float] | None (view coords)
    spanClicked = Signal(object, bool)  # hit tuple | None, double-click flag

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._canvas = PageCanvas(self)
        self.setObjectName("pdfViewer")
        self.setWidget(self._canvas)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self._current_page = 0
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._canvas.hoverMoved.connect(self.hoverMoved.emit)
        self._canvas.spanClicked.connect(self.spanClicked.emit)

    @property
    def canvas(self) -> PageCanvas:
        """The painted page canvas (overlay host for Inspect/Edit mode)."""
        return self._canvas

    # --- document ---------------------------------------------------------

    def set_session(self, session: PdfSession | None) -> None:
        self._canvas.set_session(session)
        self._current_page = 0
        self.emit_visible_page()
        self.zoomChanged.emit(self._canvas.zoom)
        if session is not None:
            self.scroll_to_page(0)

    def session(self) -> PdfSession | None:
        return self._canvas.session()

    def refresh(self) -> None:
        """Re-render after a document mutation (edit, undo, redo)."""
        self._canvas.refresh_document()

    # --- zoom -------------------------------------------------------------

    def set_zoom(self, zoom: float) -> None:
        self._canvas.set_zoom(zoom)
        self.zoomChanged.emit(self._canvas.zoom)

    def zoom_in(self) -> None:
        self.set_zoom(self._canvas.zoom * 1.15)

    def zoom_out(self) -> None:
        self.set_zoom(self._canvas.zoom / 1.15)

    def actual_size(self) -> None:
        self.set_zoom(1.0)

    def fit_width(self) -> None:
        session = self._canvas.session()
        if session is None:
            return
        page_width = self._canvas.max_page_width_pt()
        if page_width <= 0:
            return
        usable = self.viewport().width() - 2 * MARGIN_PT
        if usable > 0:
            self.set_zoom(usable / page_width)
            self.scroll_to_page(self._current_page)

    # --- navigation -------------------------------------------------------

    def scroll_to_page(self, index: int) -> None:
        count = self._canvas.page_count()
        if count == 0:
            return
        index = max(0, min(count - 1, index))
        top = self._canvas.page_top(index)
        bar = self.verticalScrollBar()
        # page tops already include the top margin
        bar.setValue(max(0, top - 1))
        self._current_page = index
        self.emit_visible_page()

    def current_page(self) -> int:
        return self._current_page

    def page_count(self) -> int:
        return self._canvas.page_count()

    def _on_scroll(self, value: int) -> None:
        page = self._canvas.page_at_y(value + 4)
        if page != self._current_page:
            self._current_page = page
            self.emit_visible_page()

    def emit_visible_page(self) -> None:
        self.visiblePageChanged.emit(self._current_page)

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Ctrl+wheel zooms; plain wheel scrolls (default behaviour)."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self._canvas.wheel_zoom(delta)
                self.zoomChanged.emit(self._canvas.zoom)
                event.accept()
            return
        super().wheelEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.session() is not None:
            self.emit_visible_page()

    @property
    def zoom(self) -> float:
        """Current zoom factor of the canvas."""
        return self._canvas.zoom


def probe_first_render_ms(session: PdfSession, zoom: float = 1.0) -> float:
    """Diagnostic: milliseconds to render page 1 at ``zoom`` (uncached)."""
    start = time.perf_counter()
    page = session.mupdf.load_page(0)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    _ = len(pix.samples)
    return (time.perf_counter() - start) * 1000.0
