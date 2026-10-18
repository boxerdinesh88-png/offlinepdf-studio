"""Lazy thumbnail list: page icons are rendered on demand and LRU-cached."""

from __future__ import annotations

import logging
from typing import Any

import pymupdf
from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import QListView, QSizePolicy, QWidget

from offlinepdf.core.document import PdfSession

__all__ = ["ThumbnailList", "ThumbnailModel"]

logger = logging.getLogger(__name__)

THUMB_H = 96  # pixels
THUMB_W = 120  # landscape pages fit inside THUMB_W x THUMB_H instead of being cut
_CACHE_MAX = 240
_NO_INDEX = QModelIndex()  # B008-safe default for Qt model overrides


class ThumbnailModel(QAbstractListModel):
    """Page-number list with lazily rendered icons."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session: PdfSession | None = None
        self._cache: dict[int, QPixmap] = {}
        self._order: list[int] = []
        self._previews: dict[str, QPixmap] = {}

    def set_session(self, session: PdfSession | None) -> None:
        self.beginResetModel()
        self._session = session
        self._cache.clear()
        self._order.clear()
        self._previews.clear()
        self.endResetModel()

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = _NO_INDEX
    ) -> int:
        if parent.isValid() or self._session is None:
            return 0
        return self._session.page_count

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or self._session is None:
            return None
        page = index.row()
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbnail(page)
        if role == Qt.ItemDataRole.DisplayRole:
            return str(page + 1)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"Page {page + 1}"
        return None

    def _thumbnail(self, page: int) -> QPixmap | None:
        if self._session is None or self._session.is_closed:
            return None
        cached = self._cache.get(page)
        if cached is not None:
            self._cache[page] = self._cache.pop(page)  # LRU touch
            return cached
        try:
            doc = self._session.mupdf
            mupdf_page = doc.load_page(page)
            rect = mupdf_page.rect
            if rect.height <= 0 or rect.width <= 0:
                return None
            scale = min(THUMB_H / rect.height, THUMB_W / rect.width)
            layer = self._session.images
            behind = layer.has_below(page)
            pix = mupdf_page.get_pixmap(
                matrix=pymupdf.Matrix(scale, scale), alpha=behind
            )
            fmt = (
                QImage.Format.Format_RGBA8888_Premultiplied
                if behind
                else QImage.Format.Format_RGB888
            )
            image = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)
            page_image = QPixmap.fromImage(image.copy())
            pixmap = QPixmap(page_image.size())
            pixmap.fill(QColor(255, 255, 255))
            painter = QPainter(pixmap)
            # Inserted images, in stacking order around the page content.
            base = QTransform().scale(scale, scale)
            items = layer.on_page(page)
            for item in (it for it in items if not it.above_content):
                self._paint_item(painter, base, item)
            painter.drawPixmap(0, 0, page_image)
            for item in (it for it in items if it.above_content):
                self._paint_item(painter, base, item)
            painter.setPen(QColor(0, 0, 0, 60))  # hairline edge: visible on light panels
            painter.drawRect(0, 0, pixmap.width() - 1, pixmap.height() - 1)
            painter.end()
        except Exception:
            logger.exception("thumbnail render failed for page %d", page + 1)
            return None
        self._cache[page] = pixmap
        self._order.append(page)
        while len(self._order) > _CACHE_MAX:
            drop = self._order.pop(0)
            self._cache.pop(drop, None)
        return pixmap

    def _paint_item(self, painter: QPainter, base: QTransform, item: Any) -> None:
        from offlinepdf.ui.image_tool import paint_image_item

        if self._session is None:
            return
        preview = self._previews.get(item.source)
        if preview is None:
            source = self._session.images.sources.get(item.source)
            if source is None:
                return
            image = QImage.fromData(source.data)
            if image.isNull():
                return
            preview = QPixmap.fromImage(
                image.scaled(
                    256, 256, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self._previews[item.source] = preview
        paint_image_item(painter, base, item, preview)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._order.clear()


class ThumbnailList(QListView):
    """Icon-mode page list emitting the selected page index (0-based)."""

    pageSelected = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = ThumbnailModel(self)
        self.setModel(self._model)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setIconSize(QSize(THUMB_W, THUMB_H))
        self.setGridSize(QSize(THUMB_W + 20, THUMB_H + 28))
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setSpacing(6)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.clicked.connect(self._on_clicked)

    def set_session(self, session: PdfSession | None) -> None:
        self._model.set_session(session)

    def refresh(self) -> None:
        """Re-render page icons after a document mutation."""
        self._model.clear_cache()
        count = self._model.rowCount()
        if count:
            self._model.dataChanged.emit(self._model.index(0), self._model.index(count - 1))

    def select_page(self, index: int) -> None:
        if index < 0 or index >= self._model.rowCount():
            return
        self.setCurrentIndex(self._model.index(index))
        self.scrollTo(self.currentIndex())

    def _on_clicked(self, index: QModelIndex) -> None:
        self.pageSelected.emit(index.row())
