"""Canvas overlay highlighting low-confidence OCR lines for review.

A passive :class:`~offlinepdf.ui.viewer.CanvasDelegate`: it paints amber
boxes over recognised lines whose confidence is below the threshold and
which nobody has edited yet, and never consumes mouse events.  Page blocks
are parsed once and cached until the document changes.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QMouseEvent, QPainter, QPen, QPolygonF

from offlinepdf.core.document import PdfSession
from offlinepdf.core.ocr.textlayer import OcrBlockInfo, page_ocr_blocks
from offlinepdf.ui.viewer import PageCanvas, PageGeom

__all__ = ["OcrReviewOverlay"]

logger = logging.getLogger(__name__)

_FILL = QColor(255, 170, 0, 60)
_EDGE = QColor(230, 120, 0, 200)


class OcrReviewOverlay:
    """Highlights uncertain OCR words (or whole lines) below ``threshold``."""

    def __init__(self, canvas: PageCanvas, threshold: float = 0.70) -> None:
        self._canvas = canvas
        self._session: PdfSession | None = None
        self._cache: dict[int, list[OcrBlockInfo]] = {}
        self.threshold = threshold
        self.enabled = True
        canvas.add_delegate(self)

    def set_session(self, session: PdfSession | None) -> None:
        self._session = session
        self.invalidate()

    def invalidate(self) -> None:
        self._cache.clear()
        self._canvas.update()

    def uncertain_quads(self, page: int) -> list[list[tuple[float, float]]]:
        """Quads to highlight: uncertain words, or whole uncertain lines."""
        session = self._session
        if session is None or session.is_closed or not 0 <= page < session.page_count:
            return []
        quads: list[list[tuple[float, float]]] = []
        for block in self._blocks(page):
            if block.edited:
                continue
            words = block.words
            if words:
                quads += [quad for _w, conf, quad in words if conf < self.threshold]
            elif block.confidence < self.threshold:
                quads.append(block.quad)
        return quads

    def _blocks(self, page: int) -> list[OcrBlockInfo]:
        blocks = self._cache.get(page)
        if blocks is None:
            try:
                blocks = page_ocr_blocks(self._session.pdf, page)  # type: ignore[union-attr]
            except Exception:  # review is best effort
                logger.exception("could not read OCR blocks of page %d", page + 1)
                blocks = []
            self._cache[page] = blocks
        return blocks

    def low_confidence(self, page: int) -> list[OcrBlockInfo]:
        """Unedited blocks whose line-level confidence is below the threshold."""
        session = self._session
        if session is None or session.is_closed or not 0 <= page < session.page_count:
            return []
        return [b for b in self._blocks(page) if not b.edited and b.confidence < self.threshold]

    # -- CanvasDelegate ---------------------------------------------------- #
    def page_needs_alpha(self, index: int) -> bool:
        return False

    def paint_layer(self, painter: QPainter, geom: PageGeom, layer: str) -> None:
        if layer != "above" or not self.enabled:
            return
        zoom = self._canvas.zoom
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(_EDGE, 1.2, Qt.PenStyle.DashLine))
        painter.setBrush(QBrush(_FILL))
        for quad in self.uncertain_quads(geom.index):
            if len(quad) != 4:
                continue
            painter.drawPolygon(
                QPolygonF([QPointF(geom.x + x * zoom, geom.y + y * zoom) for x, y in quad])
            )

    def mouse_press(self, event: QMouseEvent) -> bool:
        return False

    def mouse_move(self, event: QMouseEvent) -> bool:
        return False

    def mouse_release(self, event: QMouseEvent) -> bool:
        return False

    def mouse_double_click(self, event: QMouseEvent) -> bool:
        return False
