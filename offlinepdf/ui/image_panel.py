"""Image properties dock: exact geometry, opacity, crop, order, style.

Shows the image selected in :class:`~offlinepdf.ui.image_tool.ImageController`
and forwards every edit to it (so each change is one undoable command).
Field edits commit on Enter / focus-out / spin-arrow steps; slider and spin
runs merge into a single undo step until the control is released.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from offlinepdf.core.image_ops import ExportOptions, ImageItem
from offlinepdf.ui.image_tool import ImageController

__all__ = ["ImagePanel"]

_RANGE_PT = 20000.0


def _spin(suffix: str, low: float, high: float, decimals: int = 1) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(low, high)
    spin.setDecimals(decimals)
    spin.setSuffix(suffix)
    spin.setKeyboardTracking(False)
    spin.setAccelerated(True)
    return spin


class ImagePanel(QScrollArea):
    """Properties of the selected image plus document-wide export options."""

    exportOptionsChanged = Signal(object)  # ExportOptions

    def __init__(self, controller: ImageController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._updating = False
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)

        self._info = QLabel("Select an image, or use Insert ▸ Add Image…", body)
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        self._item_box = QWidget(body)
        item_layout = QVBoxLayout(self._item_box)
        item_layout.setContentsMargins(0, 0, 0, 0)
        item_layout.addWidget(self._build_geometry())
        item_layout.addWidget(self._build_transform())
        item_layout.addWidget(self._build_crop())
        item_layout.addWidget(self._build_appearance())
        item_layout.addWidget(self._build_arrange())
        layout.addWidget(self._item_box)
        layout.addWidget(self._build_export())
        layout.addStretch(1)
        self.setWidget(body)

        controller.selectionChanged.connect(self.show_item)
        controller.cropModeChanged.connect(self._on_crop_mode)
        self.show_item(None)

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    def _build_geometry(self) -> QGroupBox:
        box = QGroupBox("Position && size (pt)")
        grid = QGridLayout(box)
        self._x = _spin("", -_RANGE_PT, _RANGE_PT)
        self._y = _spin("", -_RANGE_PT, _RANGE_PT)
        self._w = _spin("", 1.0, _RANGE_PT)
        self._h = _spin("", 1.0, _RANGE_PT)
        self._rot = _spin("°", -180.0, 180.0)
        self._rot.setWrapping(True)
        for row, (label, spin, key) in enumerate(
            (("X", self._x, "x"), ("Y", self._y, "y"), ("W", self._w, "width"),
             ("H", self._h, "height"), ("Angle", self._rot, "rotation"))
        ):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(spin, row, 1)
            spin.valueChanged.connect(self._box_slot(key, spin))
            spin.editingFinished.connect(self._controller.seal_merge)
        self._lock = QCheckBox("Lock aspect ratio")
        self._lock.setToolTip("Corner handles keep the ratio; hold Shift while dragging to invert")
        self._lock.setChecked(True)
        self._lock.toggled.connect(self._on_lock)
        grid.addWidget(self._lock, 5, 0, 1, 2)
        self._page = QSpinBox()
        self._page.setKeyboardTracking(False)
        self._page.setMinimum(1)
        self._page.valueChanged.connect(self._on_page)
        grid.addWidget(QLabel("Page"), 6, 0)
        grid.addWidget(self._page, 6, 1)
        return box

    def _build_transform(self) -> QGroupBox:
        box = QGroupBox("Rotate && flip")
        row = QGridLayout(box)
        for index, (text, tip, slot) in enumerate((
            ("⟲ 90°", "Rotate 90° left", lambda: self._controller.rotate_by(-90.0)),
            ("⟳ 90°", "Rotate 90° right", lambda: self._controller.rotate_by(90.0)),
            ("⇋", "Flip horizontal", lambda: self._controller.flip("h")),
            ("⇵", "Flip vertical", lambda: self._controller.flip("v")),
        )):
            row.addWidget(self._button(text, tip, slot), index // 2, index % 2)
        return box

    def _build_crop(self) -> QGroupBox:
        box = QGroupBox("Crop")
        row = QGridLayout(box)
        self._crop_btn = self._button("Crop", "Crop (double-click the image)",
                                      self._controller.start_crop)
        self._apply_btn = self._button("Apply", "Apply crop (Enter)", self._controller.apply_crop)
        self._cancel_btn = self._button("Cancel", "Cancel crop (Esc)",
                                        self._controller.cancel_crop)
        self._reset_btn = self._button("Reset", "Show the whole image again",
                                       self._controller.reset_crop)
        for index, widget in enumerate(
            (self._crop_btn, self._reset_btn, self._apply_btn, self._cancel_btn)
        ):
            row.addWidget(widget, index // 2, index % 2)
        return box

    def _build_appearance(self) -> QGroupBox:
        box = QGroupBox("Appearance")
        form = QFormLayout(box)
        opacity_row = QHBoxLayout()
        self._opacity = QSlider(Qt.Orientation.Horizontal)
        self._opacity.setRange(0, 100)
        self._opacity_label = QLabel("100%")
        self._opacity_label.setFixedWidth(40)
        self._opacity.valueChanged.connect(self._on_opacity)
        self._opacity.sliderReleased.connect(self._controller.seal_merge)
        opacity_row.addWidget(self._opacity, 1)
        opacity_row.addWidget(self._opacity_label)
        form.addRow("Opacity", opacity_row)

        border_row = QHBoxLayout()
        self._border = _spin(" pt", 0.0, 200.0)
        self._border.valueChanged.connect(
            lambda v: self._edit("border", border_width=float(v))
        )
        self._border.editingFinished.connect(self._controller.seal_merge)
        self._color = QPushButton()
        self._color.setFixedWidth(42)
        self._color.setToolTip("Border colour")
        self._color.clicked.connect(self._pick_color)
        border_row.addWidget(self._border, 1)
        border_row.addWidget(self._color)
        form.addRow("Border", border_row)

        self._radius = _spin(" pt", 0.0, 2000.0)
        self._radius.valueChanged.connect(lambda v: self._edit("radius", corner_radius=float(v)))
        self._radius.editingFinished.connect(self._controller.seal_merge)
        form.addRow("Corners", self._radius)
        return box

    def _build_arrange(self) -> QGroupBox:
        box = QGroupBox("Arrange")
        grid = QGridLayout(box)
        for index, (text, how) in enumerate(
            (("Forward", "forward"), ("Backward", "backward"),
             ("To front", "front"), ("To back", "back"))
        ):
            grid.addWidget(
                self._button(
                    text, f"{text} (over/under other images and page text)",
                    lambda _=False, h=how: self._controller.restack(h),
                ),
                index // 2,
                index % 2,
            )
        self._behind = QCheckBox("Behind text")
        self._behind.setToolTip("Draw the image underneath the page's own content")
        self._behind.toggled.connect(
            lambda on: self._edit(None, "send behind content" if on else "bring above content",
                                  above_content=not on)
        )
        grid.addWidget(self._behind, 2, 0, 1, 2)
        grid.addWidget(self._button("Duplicate", "Ctrl+D", self._controller.duplicate_selected),
                       3, 0)
        grid.addWidget(self._button("Delete", "Delete", self._controller.delete_selected), 3, 1)
        return box

    def _build_export(self) -> QGroupBox:
        box = QGroupBox("Export quality")
        box.setToolTip("Applies to every inserted image when the PDF is saved")
        form = QFormLayout(box)
        self._max_px = QSpinBox()
        self._max_px.setRange(0, 20000)
        self._max_px.setSingleStep(250)
        self._max_px.setSpecialValueText("No limit")
        self._max_px.setSuffix(" px")
        self._max_px.setToolTip("Downscale images whose longest side exceeds this on save")
        self._quality = QSpinBox()
        self._quality.setRange(10, 100)
        self._quality.setSuffix(" %")
        self._quality.setToolTip("JPEG quality used when an image has to be re-encoded")
        self._lossless = QCheckBox("Keep PNG/WebP lossless")
        for widget in (self._max_px, self._quality):
            widget.setKeyboardTracking(False)
            widget.valueChanged.connect(self._emit_export)
        self._lossless.toggled.connect(self._emit_export)
        form.addRow("Max size", self._max_px)
        form.addRow("JPEG quality", self._quality)
        form.addRow(self._lossless)
        return box

    @staticmethod
    def _button(text: str, tip: str, slot: Callable[..., object]) -> QPushButton:
        button = QPushButton(text)
        if tip:
            button.setToolTip(tip)
        button.clicked.connect(slot)
        return button

    # ------------------------------------------------------------------ #
    # display
    # ------------------------------------------------------------------ #
    def set_export_options(self, options: ExportOptions) -> None:
        self._updating = True
        try:
            self._max_px.setValue(options.max_px)
            self._quality.setValue(options.jpeg_quality)
            self._lossless.setChecked(options.png_lossless)
        finally:
            self._updating = False

    def show_item(self, item: object) -> None:
        self._item_box.setVisible(isinstance(item, ImageItem))
        if not isinstance(item, ImageItem):
            self._info.setText("Select an image, or use Insert ▸ Add Image… "
                               "(drag-and-drop and paste work too).")
            return
        source = self._controller.selected_source()
        if source is not None:
            dpi = source.width * (item.crop[2] - item.crop[0]) / max(item.width / 72.0, 1e-6)
            self._info.setText(
                f"{source.width}×{source.height} px {source.fmt.upper()} · "
                f"≈{dpi:.0f} dpi at this size"
            )
        self._updating = True
        try:
            self._x.setValue(item.cx - item.width / 2.0)
            self._y.setValue(item.cy - item.height / 2.0)
            self._w.setValue(item.width)
            self._h.setValue(item.height)
            self._rot.setValue(item.rotation)
            self._opacity.setValue(round(item.opacity * 100))
            self._opacity_label.setText(f"{round(item.opacity * 100)}%")
            self._border.setValue(item.border_width)
            self._radius.setValue(item.corner_radius)
            self._behind.setChecked(not item.above_content)
            self._color.setStyleSheet(f"background: {QColor(*item.border_color).name()};")
            pages = self._controller.page_count()
            self._page.setMaximum(max(1, pages))
            self._page.setValue(item.page + 1)
        finally:
            self._updating = False
        self._on_crop_mode(self._controller.in_crop_mode)

    def _on_crop_mode(self, cropping: bool) -> None:
        self._crop_btn.setEnabled(not cropping)
        self._apply_btn.setEnabled(cropping)
        self._cancel_btn.setEnabled(cropping)

    # ------------------------------------------------------------------ #
    # edits
    # ------------------------------------------------------------------ #
    def _box_slot(self, key: str, spin: QDoubleSpinBox) -> Callable[[float], None]:
        def slot(value: float) -> None:
            if not self._updating:
                self._controller.set_box(**{key: float(value)})

        return slot

    def _edit(self, merge: str | None, description: str = "image style", **changes: object) -> None:
        if not self._updating:
            self._controller.update_selected(description, merge=merge, **changes)

    def _on_opacity(self, value: int) -> None:
        self._opacity_label.setText(f"{value}%")
        self._edit("opacity", "change opacity", opacity=value / 100.0)

    def _on_lock(self, locked: bool) -> None:
        self._controller.lock_aspect = locked

    def _on_page(self, value: int) -> None:
        if not self._updating:
            self._controller.move_to_page(value - 1)

    def _pick_color(self) -> None:
        item = self._controller.selected
        if item is None:
            return
        color = QColorDialog.getColor(QColor(*item.border_color), self, "Border colour")
        if color.isValid():
            rgb = (color.red(), color.green(), color.blue())
            changes: dict[str, Any] = {"border_color": rgb}
            if item.border_width <= 0:
                changes["border_width"] = 2.0
            self._controller.update_selected("border colour", **changes)

    def _emit_export(self, *_args: object) -> None:
        if self._updating:
            return
        self.exportOptionsChanged.emit(
            ExportOptions(
                max_px=self._max_px.value(),
                jpeg_quality=self._quality.value(),
                png_lossless=self._lossless.isChecked(),
            )
        )
