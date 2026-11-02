"""On-canvas image tool: insert, select, move, resize, rotate, crop.

Installed as the viewer canvas' :class:`~offlinepdf.ui.viewer.CanvasDelegate`.
It paints the document's :class:`~offlinepdf.core.image_ops.ImageLayer`
below/above the rendered page, draws selection handles and snap guides, and
turns mouse drags into :class:`~offlinepdf.core.image_ops.ImageLayerCommand`
history entries.  While dragging, the layer is updated live and a single
command is *recorded* on release, so one gesture is one undo step.

Clicks that miss every image fall through to the text tools (Inspect/Edit).
"""

from __future__ import annotations

import functools
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QMimeData,
    QObject,
    QPointF,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
    QTransform,
)
from PySide6.QtWidgets import QApplication

from offlinepdf.core.document import PdfSession
from offlinepdf.core.image_ops import (
    MIN_SIZE_PT,
    SUPPORTED_EXTENSIONS,
    ImageError,
    ImageItem,
    ImageLayer,
    ImageLayerCommand,
    ImageSource,
    default_placement,
    display_crop,
    item_matrix,
    load_image_source,
    mat_apply,
    mat_invert,
    rotate_vec,
    source_crop,
)
from offlinepdf.ui.viewer import PageGeom, Viewer

__all__ = ["CLIPBOARD_MIME", "ImageController", "paint_image_item"]

logger = logging.getLogger(__name__)

CLIPBOARD_MIME = "application/x-offlinepdf-image"
_HANDLE_PX = 4.5  # half size of a square handle, screen pixels
_HIT_PX = 7.0
_ROTATE_OFFSET_PX = 26.0
_SNAP_PX = 6.0
_PREVIEW_MAX_PX = 2048
_NUDGE_PT = 1.0
_NUDGE_BIG_PT = 10.0
_PASTE_OFFSET_PT = 12.0
_ACCENT = QColor(30, 110, 230)
_GUIDE = QColor(230, 40, 160)

# (sx, sy) of the eight resize handles: corners then edge midpoints.
_HANDLES = ((-1, -1), (1, -1), (1, 1), (-1, 1), (0, -1), (1, 0), (0, 1), (-1, 0))


@dataclass
class _Drag:
    kind: str  # move | resize | rotate | crop | croppan
    page: int
    start: tuple[float, float]  # pointer, view space of ``page``
    item: ImageItem  # item state when the drag began
    before: tuple[ImageItem, ...]
    handle: tuple[int, int] = (0, 0)
    angle0: float = 0.0
    moved: bool = False


class ImageController(QObject):
    """Owns image selection and every image interaction for one document."""

    selectionChanged = Signal(object)  # ImageItem | None
    cropModeChanged = Signal(bool)
    statusChanged = Signal(str)
    errorOccurred = Signal(str)

    def __init__(self, viewer: Viewer, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._viewer = viewer
        self._canvas = viewer.canvas
        self._session: PdfSession | None = None
        self._selected: str | None = None
        self._drag: _Drag | None = None
        self._crop: ImageItem | None = None  # pending crop state in crop mode
        self._guides: list[tuple[int, str, float]] = []  # (page, "v"/"h", view coord)
        self._previews: dict[str, QPixmap] = {}
        self._clip: tuple[ImageItem, ImageSource, str] | None = None
        self._seal = 0  # bumps break command merging (new gesture)
        self.lock_aspect = True
        self._canvas.add_delegate(self)
        self._shortcuts: list[QShortcut] = []
        self._build_shortcuts()

    # ------------------------------------------------------------------ #
    # document lifecycle
    # ------------------------------------------------------------------ #
    def set_session(self, session: PdfSession | None) -> None:
        self._crop = None
        self._drag = None
        self._guides.clear()
        self._previews.clear()
        self._session = session
        self._set_selected(None)

    @property
    def layer(self) -> ImageLayer | None:
        return self._session.images if self._session is not None else None

    @property
    def selected(self) -> ImageItem | None:
        layer = self.layer
        return layer.get(self._selected) if layer is not None else None

    @property
    def in_crop_mode(self) -> bool:
        return self._crop is not None

    def page_count(self) -> int:
        return self._session.page_count if self._session is not None else 0

    def selected_source(self) -> ImageSource | None:
        item, layer = self.selected, self.layer
        if item is None or layer is None:
            return None
        return layer.sources.get(item.source)

    def on_history_changed(self) -> None:
        """Re-sync after any undo/redo/push (the selection may be gone)."""
        if self._selected is not None and self.selected is None:
            self._set_selected(None)
        else:
            if self._crop is not None and self._drag is None:
                self.cancel_crop()
            self.selectionChanged.emit(self.selected)
        self._canvas.update()

    def select(self, item_id: str | None) -> None:
        """Select an item by id (None clears) and scroll it into view."""
        self._set_selected(item_id)
        item = self.selected
        if item is not None and item.page != self._viewer.current_page():
            self._viewer.scroll_to_page(item.page)

    def _set_selected(self, item_id: str | None) -> None:
        if self._crop is not None and item_id != self._selected:
            self.apply_crop()
        changed = item_id != self._selected
        self._selected = item_id
        if changed:
            self._seal += 1
        for shortcut in self._shortcuts:
            shortcut.setEnabled(item_id is not None)
        self.selectionChanged.emit(self.selected)
        self._canvas.update()

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    def _push(self, description: str, after: list[ImageItem], merge: str | None = None) -> None:
        layer = self.layer
        if self._session is None or layer is None:
            return
        key = f"{merge}:{self._seal}" if merge is not None else None
        if merge is None:
            self._seal += 1
        self._session.history.push(
            ImageLayerCommand(description, layer.items, after, merge_key=key)
        )

    def seal_merge(self) -> None:
        """End the current mergeable run (e.g. slider released)."""
        self._seal += 1

    def update_selected(
        self, description: str, *, merge: str | None = None, **changes: Any
    ) -> None:
        """Apply attribute changes to the selection as one undoable step."""
        item, layer = self.selected, self.layer
        if item is None or layer is None:
            return
        new = item.with_(**changes)
        if new == item:
            return
        key = f"{merge}:{item.id}" if merge is not None else None
        self._push(description, layer.replaced(new), merge=key)

    # ------------------------------------------------------------------ #
    # inserting
    # ------------------------------------------------------------------ #
    def insert_file(
        self, path: Path, page: int | None = None, at: tuple[float, float] | None = None
    ) -> bool:
        try:
            data = Path(path).read_bytes()
        except OSError:
            self.errorOccurred.emit(f"Cannot read {Path(path).name}.")
            return False
        return self.insert_bytes(data, page=page, at=at, name=Path(path).name)

    def insert_bytes(
        self,
        data: bytes,
        page: int | None = None,
        at: tuple[float, float] | None = None,
        name: str = "image",
    ) -> bool:
        """Insert image bytes on ``page`` (default: visible page), centred or at ``at``."""
        session, layer = self._session, self.layer
        if session is None or layer is None:
            return False
        try:
            source = layer.add_source(load_image_source(data))
        except ImageError as exc:
            self.errorOccurred.emit(f"{name}: {exc}")
            return False
        page = self._viewer.current_page() if page is None else page
        page = max(0, min(session.page_count - 1, page))
        rect = session.mupdf.load_page(page).rect
        cx, cy, w, h = default_placement(source, (rect.width, rect.height), center=at)
        item = ImageItem(
            id=ImageItem.new_id(), page=page, source=source.key, cx=cx, cy=cy, width=w, height=h
        )
        self._push("insert image", layer.inserted(item))
        self.select(item.id)
        message = f"Inserted {name} ({source.width}×{source.height} px) on page {page + 1}"
        if session.is_signed:
            message += " · saving will invalidate the digital signature"
        self.statusChanged.emit(message)
        return True

    def insert_qimage(self, image: object, page: int | None = None) -> bool:
        """Insert clipboard/drag image data (a QImage or anything it accepts)."""
        qimage = image if isinstance(image, QImage) else QImage(image)  # type: ignore[call-overload]
        if qimage.isNull():
            return False
        return self.insert_bytes(_qimage_png(qimage), page=page, name="pasted image")

    # ------------------------------------------------------------------ #
    # item actions
    # ------------------------------------------------------------------ #
    def delete_selected(self) -> None:
        item, layer = self.selected, self.layer
        if item is None or layer is None:
            return
        self._crop = None
        self._push("delete image", layer.removed(item.id))
        self.statusChanged.emit("Image deleted")

    def duplicate_selected(self) -> None:
        item, layer = self.selected, self.layer
        if item is None or layer is None:
            return
        copy = item.with_(
            id=ImageItem.new_id(), cx=item.cx + _PASTE_OFFSET_PT, cy=item.cy + _PASTE_OFFSET_PT
        )
        self._push("duplicate image", layer.inserted(copy, after=item.id))
        self.select(copy.id)

    def copy_selected(self, cut: bool = False) -> bool:
        item, source = self.selected, self.selected_source()
        if item is None or source is None:
            return False
        token = ImageItem.new_id()
        self._clip = (item, source, token)
        mime = QMimeData()
        mime.setData(CLIPBOARD_MIME, QByteArray(json.dumps({"token": token}).encode()))
        image = QImage.fromData(source.data)
        if not image.isNull():
            mime.setImageData(image)
        QApplication.clipboard().setMimeData(mime)
        if cut:
            self.delete_selected()
            self.statusChanged.emit("Image cut")
        else:
            self.statusChanged.emit("Image copied")
        return True

    def paste(self, page: int | None = None) -> bool:
        """Paste a copied item (keeps crop/rotation/…) or any clipboard image."""
        session, layer = self._session, self.layer
        if session is None or layer is None:
            return False
        page = self._viewer.current_page() if page is None else page
        mime = QApplication.clipboard().mimeData()
        if mime is not None and mime.hasFormat(CLIPBOARD_MIME) and self._clip is not None:
            try:
                token = json.loads(bytes(mime.data(CLIPBOARD_MIME).data()).decode())["token"]
            except (ValueError, KeyError):
                token = None
            item, source, clip_token = self._clip
            if token == clip_token:
                layer.add_source(source)
                rect = session.mupdf.load_page(page).rect
                offset = _PASTE_OFFSET_PT if layer.get(item.id) and item.page == page else 0.0
                cx = min(max(item.cx + offset, 0.0), rect.width)
                cy = min(max(item.cy + offset, 0.0), rect.height)
                pasted = item.with_(id=ImageItem.new_id(), page=page, cx=cx, cy=cy)
                self._push("paste image", layer.inserted(pasted))
                self.select(pasted.id)
                return True
        if mime is not None and mime.hasImage():
            image = QImage(mime.imageData())
            if not image.isNull():
                return self.insert_qimage(image, page)
        if mime is not None and mime.hasUrls():
            files = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
            inserted = False
            for path in files:
                if _is_image_path(path):
                    inserted = self.insert_file(path, page=page) or inserted
            return inserted
        return False

    def can_paste(self) -> bool:
        mime = QApplication.clipboard().mimeData()
        if mime is None or self._session is None:
            return False
        return mime.hasFormat(CLIPBOARD_MIME) or mime.hasImage() or any(
            _is_image_path(Path(u.toLocalFile())) for u in mime.urls() if u.isLocalFile()
        )

    def nudge(self, dx: float, dy: float) -> None:
        item = self.selected
        if item is None or self._crop is not None:
            return
        self.update_selected("move image", merge="nudge", cx=item.cx + dx, cy=item.cy + dy)

    def rotate_by(self, degrees: float) -> None:
        item = self.selected
        if item is not None:
            self.update_selected("rotate image", rotation=_norm_angle(item.rotation + degrees))

    def flip(self, axis: str) -> None:
        item = self.selected
        if item is None:
            return
        if axis == "h":
            self.update_selected("flip image", flip_h=not item.flip_h)
        else:
            self.update_selected("flip image", flip_v=not item.flip_v)

    def restack(self, how: str) -> None:
        item, layer = self.selected, self.layer
        if item is None or layer is None:
            return
        after = layer.restacked(item.id, how)
        if after != list(layer.items):
            labels = {"front": "bring to front", "back": "send to back"}
            self._push(labels.get(how, f"move image {how}"), after)

    def move_to_page(self, page: int) -> None:
        item, session = self.selected, self._session
        if item is None or session is None or page == item.page:
            return
        if not 0 <= page < session.page_count:
            return
        rect = session.mupdf.load_page(page).rect
        self.update_selected(
            f"move image to page {page + 1}",
            page=page,
            cx=min(max(item.cx, 0.0), rect.width),
            cy=min(max(item.cy, 0.0), rect.height),
        )
        self._viewer.scroll_to_page(page)

    def set_box(
        self,
        *,
        x: float | None = None,
        y: float | None = None,
        width: float | None = None,
        height: float | None = None,
        rotation: float | None = None,
    ) -> None:
        """Exact edit from the panel; X/Y are the unrotated box's top-left."""
        item = self.selected
        if item is None:
            return
        w, h = item.width, item.height
        if width is not None:
            width = max(MIN_SIZE_PT, width)
            if self.lock_aspect and w > 0:
                h = h * width / w
            w = width
        elif height is not None:
            height = max(MIN_SIZE_PT, height)
            if self.lock_aspect and h > 0:
                w = w * height / h
            h = height
        # The top-left stays anchored on size edits, like design apps do.
        left = item.cx - item.width / 2.0 if x is None else x
        top = item.cy - item.height / 2.0 if y is None else y
        self.update_selected(
            "set image geometry",
            merge="box",
            cx=left + w / 2.0,
            cy=top + h / 2.0,
            width=w,
            height=h,
            rotation=item.rotation if rotation is None else _norm_angle(rotation),
        )

    # ------------------------------------------------------------------ #
    # crop mode
    # ------------------------------------------------------------------ #
    def start_crop(self) -> None:
        item = self.selected
        if item is None or self._crop is not None:
            return
        self._crop = item
        self.cropModeChanged.emit(True)
        self.statusChanged.emit("Crop: drag the handles or the frame · Enter applies, Esc cancels")
        self._canvas.update()

    def apply_crop(self) -> None:
        pending, item = self._crop, self.selected
        self._crop = None
        self.cropModeChanged.emit(False)
        if pending is not None and item is not None and pending != item:
            layer = self.layer
            assert layer is not None
            self._push("crop image", layer.replaced(pending))
        self._canvas.update()

    def cancel_crop(self) -> None:
        if self._crop is None:
            return
        self._crop = None
        self.cropModeChanged.emit(False)
        self._canvas.update()

    def reset_crop(self) -> None:
        """Show the whole image again (keeps the scale of the pixels)."""
        item = self._crop or self.selected
        if item is None:
            return
        full_w, full_h = item.full_size
        cx, cy = _full_center(item)
        reset = item.with_(crop=(0.0, 0.0, 1.0, 1.0), width=full_w, height=full_h, cx=cx, cy=cy)
        if self._crop is not None:
            self._crop = reset
            self._canvas.update()
        else:
            changes = _fields(reset, "crop", "width", "height", "cx", "cy")
            self.update_selected("reset crop", **changes)

    # ------------------------------------------------------------------ #
    # keyboard
    # ------------------------------------------------------------------ #
    def _build_shortcuts(self) -> None:
        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        bindings: list[tuple[QKeySequence, Callable[[], None]]] = [
            (QKeySequence(Qt.Key.Key_Delete), self.delete_selected),
            (QKeySequence(Qt.Key.Key_Backspace), self.delete_selected),
            (QKeySequence(Qt.Key.Key_Escape), self._escape),
            (QKeySequence(Qt.Key.Key_Return), self._enter),
            (QKeySequence(Qt.Key.Key_Enter), self._enter),
        ]
        for key, dx, dy in (
            (Qt.Key.Key_Left, -1, 0),
            (Qt.Key.Key_Right, 1, 0),
            (Qt.Key.Key_Up, 0, -1),
            (Qt.Key.Key_Down, 0, 1),
        ):
            small = functools.partial(self.nudge, dx * _NUDGE_PT, dy * _NUDGE_PT)
            big = functools.partial(self.nudge, dx * _NUDGE_BIG_PT, dy * _NUDGE_BIG_PT)
            bindings.append((QKeySequence(key), small))
            bindings.append((QKeySequence(Qt.KeyboardModifier.ShiftModifier | key), big))
        for sequence, slot in bindings:
            shortcut = QShortcut(sequence, self._viewer)
            shortcut.setContext(ctx)
            shortcut.activated.connect(slot)
            shortcut.setEnabled(False)
            self._shortcuts.append(shortcut)

    def _escape(self) -> None:
        if self._crop is not None:
            self.cancel_crop()
        else:
            self._set_selected(None)

    def _enter(self) -> None:
        if self._crop is not None:
            self.apply_crop()

    # ------------------------------------------------------------------ #
    # geometry helpers
    # ------------------------------------------------------------------ #
    def _view_point(self, page: int, event: QMouseEvent) -> tuple[float, float]:
        geom = self._canvas.geom(page)
        zoom = self._canvas.zoom or 1.0
        pos = event.position()
        if geom is None:
            return (0.0, 0.0)
        return ((pos.x() - geom.x) / zoom, (pos.y() - geom.y) / zoom)

    def _shown(self, item: ImageItem) -> ImageItem:
        """What is drawn for ``item`` (the pending crop while cropping it)."""
        if self._crop is not None and item.id == self._selected:
            return self._crop
        return item

    def _hit_item(self, page: int, vx: float, vy: float) -> ImageItem | None:
        layer = self.layer
        if layer is None:
            return None
        for item in reversed(layer.on_page(page)):
            lx, ly = mat_apply(mat_invert(item_matrix(item)), vx, vy)
            if 0.0 <= lx <= item.width and 0.0 <= ly <= item.height:
                return item
        return None

    def _handle_points(self, item: ImageItem) -> list[tuple[tuple[int, int], float, float]]:
        hw, hh = item.width / 2.0, item.height / 2.0
        out = []
        for sx, sy in _HANDLES:
            dx, dy = rotate_vec(sx * hw, sy * hh, item.rotation)
            out.append(((sx, sy), item.cx + dx, item.cy + dy))
        return out

    def _rotate_point(self, item: ImageItem) -> tuple[float, float]:
        offset = item.height / 2.0 + _ROTATE_OFFSET_PX / (self._canvas.zoom or 1.0)
        dx, dy = rotate_vec(0.0, -offset, item.rotation)
        return (item.cx + dx, item.cy + dy)

    def _hit_handle(
        self, item: ImageItem, vx: float, vy: float
    ) -> tuple[str, tuple[int, int]] | None:
        tol = _HIT_PX / (self._canvas.zoom or 1.0)
        if self._crop is None:
            rx, ry = self._rotate_point(item)
            if math.hypot(vx - rx, vy - ry) <= tol:
                return ("rotate", (0, 0))
        for handle, hx, hy in self._handle_points(item):
            if abs(vx - hx) <= tol and abs(vy - hy) <= tol:
                return ("crop" if self._crop is not None else "resize", handle)
        return None

    # ------------------------------------------------------------------ #
    # CanvasDelegate: mouse
    # ------------------------------------------------------------------ #
    def mouse_press(self, event: QMouseEvent) -> bool:
        if self._session is None or event.button() != Qt.MouseButton.LeftButton:
            return False
        pos = event.position()
        hit = self._canvas.locate(pos.x(), pos.y())
        current = self.selected
        layer = self.layer
        assert layer is not None
        # Handles of the selection win, even slightly outside the page.
        if current is not None:
            shown = self._shown(current)
            vx, vy = self._view_point(current.page, event)
            handle = self._hit_handle(shown, vx, vy)
            if handle is not None:
                kind, which = handle
                self._begin(kind, current.page, (vx, vy), shown, which)
                return True
            if self._crop is not None:
                lx, ly = mat_apply(mat_invert(item_matrix(shown)), vx, vy)
                if 0.0 <= lx <= shown.width and 0.0 <= ly <= shown.height:
                    self._begin("croppan", current.page, (vx, vy), shown)
                    return True
                self.apply_crop()
        if hit is None:
            if current is not None:
                self._set_selected(None)
            return False
        page, vx, vy = hit
        item = self._hit_item(page, vx, vy)
        if item is None:
            if current is not None:
                self._set_selected(None)
            return False
        if item.id != self._selected:
            self._set_selected(item.id)
        self._canvas.setFocus(Qt.FocusReason.MouseFocusReason)
        self._begin("move", page, (vx, vy), item)
        return True

    def _begin(
        self,
        kind: str,
        page: int,
        start: tuple[float, float],
        item: ImageItem,
        handle: tuple[int, int] = (0, 0),
    ) -> None:
        layer = self.layer
        assert layer is not None
        angle0 = 0.0
        if kind == "rotate":
            angle0 = _pointer_angle(item, start) - item.rotation
        self._drag = _Drag(kind, page, start, item, layer.items, handle, angle0)

    def mouse_move(self, event: QMouseEvent) -> bool:
        drag = self._drag
        if drag is None:
            self._update_cursor(event)
            return False
        if not event.buttons() & Qt.MouseButton.LeftButton:
            self.mouse_release(event)
            return True
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        vx, vy = self._view_point(drag.page, event)
        item = drag.item
        if not drag.moved:
            zoom = self._canvas.zoom or 1.0
            if math.hypot(vx - drag.start[0], vy - drag.start[1]) * zoom < 2.0:
                return True
            drag.moved = True
        self._guides.clear()
        if drag.kind == "move":
            new = self._drag_move(drag, event, snap=not alt)
        elif drag.kind == "resize":
            new = _resize(item, drag.handle, (vx, vy), keep_ratio=self.lock_aspect != shift)
        elif drag.kind == "rotate":
            angle = _pointer_angle(item, (vx, vy)) - drag.angle0
            if shift:
                angle = round(angle / 15.0) * 15.0
            new = item.with_(rotation=_norm_angle(angle))
        elif drag.kind == "crop":
            new = _crop_drag(item, drag.handle, (vx, vy), None)
        else:  # croppan
            new = _crop_drag(item, (0, 0), (vx, vy), drag.start)
        if drag.kind in ("crop", "croppan"):
            self._crop = new
        else:
            layer = self.layer
            assert layer is not None
            layer.set_items(layer.replaced(new))
            self.selectionChanged.emit(new)
        self._canvas.update()
        return True

    def _drag_move(self, drag: _Drag, event: QMouseEvent, *, snap: bool) -> ImageItem:
        item = drag.item
        page = drag.page
        pos = event.position()
        hit = self._canvas.locate(pos.x(), pos.y())
        grab = (drag.start[0] - item.cx, drag.start[1] - item.cy)
        if hit is not None and hit[0] != item.page:
            # Dragged onto another page: carry the image over.
            page = hit[0]
            vx, vy = hit[1], hit[2]
        else:
            page = item.page
            vx, vy = self._view_point(page, event)
        cx, cy = vx - grab[0], vy - grab[1]
        moved = item.with_(page=page, cx=cx, cy=cy)
        if snap:
            moved = self._snap(moved)
        return moved

    def _snap(self, item: ImageItem) -> ImageItem:
        session, layer = self._session, self.layer
        if session is None or layer is None:
            return item
        rect = session.mupdf.load_page(item.page).rect
        tol = _SNAP_PX / (self._canvas.zoom or 1.0)
        ex, ey = _half_extents(item)
        xs, ys = [0.0, rect.width / 2.0, rect.width], [0.0, rect.height / 2.0, rect.height]
        for other in layer.on_page(item.page):
            if other.id == item.id:
                continue
            ox, oy = _half_extents(other)
            xs += [other.cx - ox, other.cx, other.cx + ox]
            ys += [other.cy - oy, other.cy, other.cy + oy]
        cx, cy = item.cx, item.cy
        best_x = min(
            ((t - p, t) for t in xs for p in (cx - ex, cx, cx + ex)), key=lambda d: abs(d[0])
        )
        best_y = min(
            ((t - p, t) for t in ys for p in (cy - ey, cy, cy + ey)), key=lambda d: abs(d[0])
        )
        if abs(best_x[0]) <= tol:
            cx += best_x[0]
            self._guides.append((item.page, "v", best_x[1]))
        if abs(best_y[0]) <= tol:
            cy += best_y[0]
            self._guides.append((item.page, "h", best_y[1]))
        return item.with_(cx=cx, cy=cy)

    def mouse_release(self, event: QMouseEvent) -> bool:
        drag = self._drag
        if drag is None:
            return False
        self._drag = None
        self._guides.clear()
        layer, session = self.layer, self._session
        if drag.moved and drag.kind not in ("crop", "croppan") and layer and session:
            labels = {"move": "move image", "resize": "resize image", "rotate": "rotate image"}
            self._seal += 1
            session.history.record(ImageLayerCommand(labels[drag.kind], drag.before, layer.items))
            session.mark_dirty()
        self._canvas.update()
        return True

    def mouse_double_click(self, event: QMouseEvent) -> bool:
        item = self.selected
        if item is None or self._session is None:
            return False
        vx, vy = self._view_point(item.page, event)
        if self._hit_item(item.page, vx, vy) is not None and self._crop is None:
            self.start_crop()
            return True
        return self._crop is not None

    def _update_cursor(self, event: QMouseEvent) -> None:
        if self._session is None:
            return
        item = self.selected
        cursor: Qt.CursorShape | None = None
        if item is not None:
            shown = self._shown(item)
            vx, vy = self._view_point(item.page, event)
            handle = self._hit_handle(shown, vx, vy)
            if handle is not None:
                kind, (sx, sy) = handle
                if kind == "rotate":
                    cursor = Qt.CursorShape.PointingHandCursor
                else:
                    cursor = _resize_cursor(sx, sy, shown.rotation)
        if cursor is None:
            pos = event.position()
            hit = self._canvas.locate(pos.x(), pos.y())
            if hit is not None and self._hit_item(*hit) is not None:
                cursor = Qt.CursorShape.SizeAllCursor
        if cursor is None:
            self._canvas.unsetCursor()
        else:
            self._canvas.setCursor(QCursor(cursor))

    # ------------------------------------------------------------------ #
    # CanvasDelegate: painting
    # ------------------------------------------------------------------ #
    def page_needs_alpha(self, index: int) -> bool:
        layer = self.layer
        return layer is not None and layer.has_below(index)

    def paint_layer(self, painter: QPainter, geom: PageGeom, layer_name: str) -> None:
        layer = self.layer
        if layer is None:
            return
        if layer_name == "chrome":
            self._paint_chrome(painter, geom)
            return
        above = layer_name == "above"
        for item in layer.on_page(geom.index):
            if item.above_content != above:
                continue
            shown = self._shown(item)
            if shown is not item:
                self._draw_item(painter, geom, _ghost(shown))
            self._draw_item(painter, geom, shown)

    def _base(self, geom: PageGeom) -> QTransform:
        zoom = self._canvas.zoom
        return QTransform().translate(geom.x, geom.y).scale(zoom, zoom)

    def _preview(self, key: str) -> QPixmap | None:
        pix = self._previews.get(key)
        if pix is not None:
            return pix
        layer = self.layer
        source = layer.sources.get(key) if layer is not None else None
        if source is None:
            return None
        image = QImage.fromData(source.data)
        if image.isNull():
            return None
        if max(image.width(), image.height()) > _PREVIEW_MAX_PX:
            image = image.scaled(
                _PREVIEW_MAX_PX,
                _PREVIEW_MAX_PX,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        pix = QPixmap.fromImage(image)
        self._previews[key] = pix
        return pix

    def _draw_item(self, painter: QPainter, geom: PageGeom, item: ImageItem) -> None:
        pix = self._preview(item.source)
        if pix is not None:
            paint_image_item(painter, self._base(geom), item, pix)

    def _paint_chrome(self, painter: QPainter, geom: PageGeom) -> None:
        zoom = self._canvas.zoom
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for page, orient, value in self._guides:
            if page != geom.index:
                continue
            pen = QPen(_GUIDE, 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            if orient == "v":
                x = geom.x + value * zoom
                painter.drawLine(QPointF(x, geom.y), QPointF(x, geom.bottom))
            else:
                y = geom.y + value * zoom
                painter.drawLine(QPointF(geom.x, y), QPointF(geom.x + geom.width, y))
        item = self.selected
        if item is None or item.page != geom.index:
            return
        shown = self._shown(item)

        def canvas_pt(vx: float, vy: float) -> QPointF:
            return QPointF(geom.x + vx * zoom, geom.y + vy * zoom)

        points = self._handle_points(shown)
        outline = [canvas_pt(x, y) for _, x, y in points[:4]]
        style = Qt.PenStyle.DashLine if self._crop else Qt.PenStyle.SolidLine
        painter.setPen(QPen(_ACCENT, 1.5, style))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(outline)
        if self._crop is None:
            top_mid = canvas_pt(points[4][1], points[4][2])
            rot = canvas_pt(*self._rotate_point(shown))
            painter.setPen(QPen(_ACCENT, 1.2))
            painter.drawLine(top_mid, rot)
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            painter.drawEllipse(rot, _HANDLE_PX + 1, _HANDLE_PX + 1)
        painter.setPen(QPen(_ACCENT, 1.2))
        painter.setBrush(QBrush(QColor(20, 20, 20) if self._crop else QColor(255, 255, 255)))
        for _, x, y in points:
            p = canvas_pt(x, y)
            size = 2 * _HANDLE_PX
            painter.drawRect(QRectF(p.x() - _HANDLE_PX, p.y() - _HANDLE_PX, size, size))


# ---------------------------------------------------------------------- #
# pure geometry (kept module-level so tests can exercise it)
# ---------------------------------------------------------------------- #
def paint_image_item(painter: QPainter, base: QTransform, item: ImageItem, pix: QPixmap) -> None:
    """Draw one placed image (crop, rotation, flips, opacity, border, corners).

    ``base`` maps view-space points to the paint device; shared by the page
    canvas and the thumbnails so both show exactly what will be saved.
    """
    painter.save()
    painter.setRenderHints(
        QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
    )
    painter.setTransform(QTransform(*item_matrix(item)) * base)
    painter.setOpacity(max(0.0, min(1.0, item.opacity)))
    full_w, full_h = item.full_size
    target = QRectF(-item.crop[0] * full_w, -item.crop[1] * full_h, full_w, full_h)
    painter.save()
    painter.setClipPath(
        _rounded(0.0, 0.0, item.width, item.height, item.corner_radius),
        Qt.ClipOperation.IntersectClip,
    )
    painter.drawPixmap(target, pix, QRectF(pix.rect()))
    painter.restore()
    bw = min(item.border_width, item.width / 2.0, item.height / 2.0)
    if bw > 0:
        pen = QPen(QColor(*item.border_color), bw)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        half = bw / 2.0
        radius = max(0.0, item.corner_radius - half)
        painter.drawPath(_rounded(half, half, item.width - bw, item.height - bw, radius))
    painter.restore()


def _is_image_path(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def _qimage_png(image: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")  # PySide6 6.11 rejects a bytes format here
    buf.close()
    return bytes(data.data())


def _norm_angle(degrees: float) -> float:
    angle = math.fmod(degrees, 360.0)
    if angle > 180.0:
        angle -= 360.0
    elif angle <= -180.0:
        angle += 360.0
    return round(angle, 4)


def _pointer_angle(item: ImageItem, point: tuple[float, float]) -> float:
    return math.degrees(math.atan2(point[1] - item.cy, point[0] - item.cx)) + 90.0


def _half_extents(item: ImageItem) -> tuple[float, float]:
    rad = math.radians(item.rotation)
    c, s = abs(math.cos(rad)), abs(math.sin(rad))
    hw, hh = item.width / 2.0, item.height / 2.0
    return (hw * c + hh * s, hw * s + hh * c)


def _resize(
    item: ImageItem, handle: tuple[int, int], pointer: tuple[float, float], *, keep_ratio: bool
) -> ImageItem:
    """Drag ``handle`` to ``pointer`` with the opposite handle pinned."""
    sx, sy = handle
    hw, hh = item.width / 2.0, item.height / 2.0
    ax, ay = rotate_vec(-sx * hw, -sy * hh, item.rotation)
    anchor = (item.cx + ax, item.cy + ay)
    lx, ly = rotate_vec(pointer[0] - anchor[0], pointer[1] - anchor[1], -item.rotation)
    w = max(MIN_SIZE_PT, sx * lx) if sx else item.width
    h = max(MIN_SIZE_PT, sy * ly) if sy else item.height
    if keep_ratio and sx and sy:
        scale = max(w / item.width, h / item.height)
        w, h = item.width * scale, item.height * scale
    ox, oy = rotate_vec(sx * w / 2.0, sy * h / 2.0, item.rotation)
    return item.with_(cx=anchor[0] + ox, cy=anchor[1] + oy, width=w, height=h)


def _full_center(item: ImageItem) -> tuple[float, float]:
    """View-space centre of the whole (uncropped) image."""
    full_w, full_h = item.full_size
    left, top, right, bottom = display_crop(item)
    dx = (0.5 - (left + right) / 2.0) * full_w
    dy = (0.5 - (top + bottom) / 2.0) * full_h
    ox, oy = rotate_vec(dx, dy, item.rotation)
    return (item.cx + ox, item.cy + oy)


def _crop_drag(
    item: ImageItem,
    handle: tuple[int, int],
    pointer: tuple[float, float],
    pan_from: tuple[float, float] | None,
) -> ImageItem:
    """New pending crop for a handle drag, or a frame pan when ``pan_from`` is set."""
    full_w, full_h = item.full_size
    fcx, fcy = _full_center(item)

    def frac(point: tuple[float, float]) -> tuple[float, float]:
        lx, ly = rotate_vec(point[0] - fcx, point[1] - fcy, -item.rotation)
        return (lx / full_w + 0.5, ly / full_h + 0.5)

    left, top, right, bottom = display_crop(item)
    min_x, min_y = MIN_SIZE_PT / full_w, MIN_SIZE_PT / full_h
    fx, fy = frac(pointer)
    if pan_from is not None:
        px, py = frac(pan_from)
        dx = min(max(fx - px, -left), 1.0 - right)
        dy = min(max(fy - py, -top), 1.0 - bottom)
        left, right, top, bottom = left + dx, right + dx, top + dy, bottom + dy
    else:
        sx, sy = handle
        if sx < 0:
            left = min(max(fx, 0.0), right - min_x)
        elif sx > 0:
            right = max(min(fx, 1.0), left + min_x)
        if sy < 0:
            top = min(max(fy, 0.0), bottom - min_y)
        elif sy > 0:
            bottom = max(min(fy, 1.0), top + min_y)
    shown = (left, top, right, bottom)
    ox, oy = rotate_vec(
        ((left + right) / 2.0 - 0.5) * full_w, ((top + bottom) / 2.0 - 0.5) * full_h, item.rotation
    )
    return item.with_(
        crop=source_crop(item, shown),
        width=(right - left) * full_w,
        height=(bottom - top) * full_h,
        cx=fcx + ox,
        cy=fcy + oy,
    )


def _ghost(item: ImageItem) -> ImageItem:
    """The whole image, faded, drawn behind the crop frame."""
    full_w, full_h = item.full_size
    cx, cy = _full_center(item)
    return item.with_(
        crop=(0.0, 0.0, 1.0, 1.0),
        width=full_w,
        height=full_h,
        cx=cx,
        cy=cy,
        corner_radius=0.0,
        border_width=0.0,
        opacity=0.35 * item.opacity,
    )


def _fields(item: ImageItem, *names: str) -> dict[str, Any]:
    return {name: getattr(item, name) for name in names}


def _rounded(x: float, y: float, w: float, h: float, r: float) -> QPainterPath:
    path = QPainterPath()
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    if r > 0:
        path.addRoundedRect(QRectF(x, y, w, h), r, r)
    else:
        path.addRect(QRectF(x, y, w, h))
    return path


def _resize_cursor(sx: int, sy: int, rotation: float) -> Qt.CursorShape:
    angle = (math.degrees(math.atan2(sy, sx)) + rotation) % 180.0
    shapes = (
        Qt.CursorShape.SizeHorCursor,
        Qt.CursorShape.SizeFDiagCursor,
        Qt.CursorShape.SizeVerCursor,
        Qt.CursorShape.SizeBDiagCursor,
    )
    return shapes[round(angle / 45.0) % 4]
