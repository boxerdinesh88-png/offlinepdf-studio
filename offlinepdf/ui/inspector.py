"""Right-hand dock: style inspector (M2) + document info and warnings.

The Style tab shows what the detection pipeline found on the current page:

* eight detected fields, each with a confidence bar and origin tag
* the six document presets (Body, H1..H3, Caption, Footer) as a table
* glyph coverage for the page and the whole document
* performance numbers (model / detect / render milliseconds)
* a Copy JSON button (profile + coverage + perf, for bug reports)
* a Debug overlay checkbox (XY-cut rectangles and baselines in the viewer)
* the auto-match checkbox (editing aid, arrives fully in M3)

Everything is pushed in from ``ui.main_window`` - this widget holds no
document logic of its own.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from offlinepdf.core.document import PdfSession
from offlinepdf.core.glyph_coverage import DocumentCoverage, PageCoverage
from offlinepdf.core.style_detector import StyleProfile

_PRESETS = ("Body", "H1", "H2", "H3", "Caption", "Footer")


def _origin_tag(origin: str) -> str:
    return {
        "detected": "detected",
        "inherited": "inherited",
        "user_override": "user",
    }.get(origin, origin)


class _FieldRow(QWidget):
    """label + value + confidence bar + origin tag."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.setSpacing(6)
        self._label = QLabel(label)
        self._label.setMinimumWidth(84)
        self._value = QLabel("-")
        self._value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setFixedHeight(10)
        self._bar.setTextVisible(True)
        self._bar.setFormat("%p%")
        self._origin = QLabel("")
        self._origin.setMinimumWidth(58)
        self._origin.setProperty("role", "muted")
        self._origin.setStyleSheet("font-size: 10px;")
        layout.addWidget(self._label)
        layout.addWidget(self._value, 1)
        layout.addWidget(self._bar, 1)
        layout.addWidget(self._origin)

    def set_field(self, value: str, confidence: float, origin: str) -> None:
        self._value.setText(value)
        self._bar.setValue(max(0, min(100, round(confidence * 100))))
        self._origin.setText(_origin_tag(origin))


class StyleInspector(QWidget):
    """Two tabs: the (M2) style panel and always-available document info."""

    autoMatchToggled = Signal(bool)
    debugOverlayToggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._doc: PdfSession | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self._tabs = QTabWidget(self)
        layout.addWidget(self._tabs)

        self._tabs.addTab(self._build_style_tab(), self.tr("Style"))
        self._tabs.addTab(self._build_document_tab(), self.tr("Document"))

    # ------------------------------------------------------------------ #
    # style tab
    # ------------------------------------------------------------------ #
    def _build_style_tab(self) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        self._progress_label = QLabel("")
        self._progress_label.setProperty("role", "muted")
        self._progress_label.setStyleSheet("font-size: 11px;")
        self._progress_label.setVisible(False)
        outer.addWidget(self._progress_label)

        self._no_text_label = QLabel(
            self.tr(
                "This page has no text layer (scanned image?). "
                "Style detection is unavailable here."
            )
        )
        self._no_text_label.setWordWrap(True)
        self._no_text_label.setProperty("role", "notice")
        self._no_text_label.setVisible(False)
        outer.addWidget(self._no_text_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._style_layout = QVBoxLayout(inner)
        self._style_layout.setContentsMargins(0, 0, 0, 0)
        self._style_layout.setSpacing(6)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # detected fields group
        fields_group = QGroupBox(self.tr("Detected style"))
        grid = QGridLayout(fields_group)
        grid.setContentsMargins(6, 8, 6, 6)
        grid.setHorizontalSpacing(4)
        self._rows: dict[str, _FieldRow] = {}
        field_specs = (
            ("font_family", self.tr("Font")),
            ("weight", self.tr("Weight")),
            ("italic", self.tr("Italic")),
            ("size", self.tr("Size")),
            ("line_height", self.tr("Line height")),
            ("color_srgb", self.tr("Colour")),
            ("alignment", self.tr("Alignment")),
            ("decorations", self.tr("Decorations")),
        )
        for row_index, (key, label) in enumerate(field_specs):
            row = _FieldRow(label)
            self._rows[key] = row
            grid.addWidget(row, row_index, 0)
        self._style_layout.addWidget(fields_group)

        # presets group
        presets_group = QGroupBox(self.tr("Document presets"))
        presets_layout = QVBoxLayout(presets_group)
        presets_layout.setContentsMargins(6, 8, 6, 6)
        self._presets_table = QTableWidget(len(_PRESETS), 6)
        self._presets_table.setHorizontalHeaderLabels(
            [
                self.tr("Preset"),
                self.tr("Status"),
                self.tr("Family"),
                self.tr("Size"),
                self.tr("Align"),
                self.tr("Conf"),
            ]
        )
        self._presets_table.verticalHeader().setVisible(False)
        self._presets_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._presets_table.setSelectionMode(
            QTableWidget.SelectionMode.NoSelection
        )
        self._presets_table.setAlternatingRowColors(True)
        self._presets_table.setMaximumHeight(190)
        for row_index, name in enumerate(_PRESETS):
            item = QTableWidgetItem(name)
            self._presets_table.setItem(row_index, 0, item)
        presets_layout.addWidget(self._presets_table)
        self._style_layout.addWidget(presets_group)

        # glyph coverage group
        coverage_group = QGroupBox(self.tr("Glyph coverage"))
        coverage_layout = QVBoxLayout(coverage_group)
        coverage_layout.setContentsMargins(6, 8, 6, 6)
        self._coverage_label = QLabel(self.tr("No coverage data yet."))
        self._coverage_label.setWordWrap(True)
        self._coverage_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._coverage_label.setStyleSheet("font-size: 11px;")
        coverage_layout.addWidget(self._coverage_label)
        self._style_layout.addWidget(coverage_group)

        # performance group
        perf_group = QGroupBox(self.tr("Performance"))
        perf_layout = QVBoxLayout(perf_group)
        perf_layout.setContentsMargins(6, 8, 6, 6)
        self._perf_label = QLabel("-")
        self._perf_label.setWordWrap(True)
        self._perf_label.setStyleSheet("font-size: 11px;")
        perf_layout.addWidget(self._perf_label)
        self._style_layout.addWidget(perf_group)

        self._style_layout.addStretch(1)

        # controls
        controls = QHBoxLayout()
        self._debug_check = QCheckBox(self.tr("Debug overlay"))
        self._debug_check.setToolTip(
            self.tr("Draw XY-cut blocks, spans, chars and baselines on the page.")
        )
        self._debug_check.toggled.connect(self.debugOverlayToggled.emit)
        controls.addWidget(self._debug_check)
        controls.addStretch(1)
        self._copy_json = QPushButton(self.tr("Copy JSON"))
        self._copy_json.setToolTip(
            self.tr("Copy the full detection result (style, coverage, perf).")
        )
        self._copy_json.clicked.connect(self._copy_json_to_clipboard)
        controls.addWidget(self._copy_json)
        outer.addLayout(controls)

        self._auto_match = QCheckBox(self.tr("Auto-match style (recommended)"))
        self._auto_match.setChecked(True)
        self._auto_match.setToolTip(
            self.tr("New and edited text automatically inherits the surrounding style.")
        )
        self._auto_match.toggled.connect(self.autoMatchToggled.emit)
        outer.addWidget(self._auto_match)

        self._profile: StyleProfile | None = None
        self._page_coverage: PageCoverage | None = None
        self._doc_coverage: DocumentCoverage | None = None
        self._perf: dict = {}
        return container

    # ------------------------------------------------------------------ #
    # document tab (unchanged from M1)
    # ------------------------------------------------------------------ #
    def _build_document_tab(self) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(4, 4, 4, 4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._doc_layout = QFormLayout(inner)
        self._doc_layout.setContentsMargins(0, 0, 0, 0)
        self._doc_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        self._warnings_label = QLabel("")
        self._warnings_label.setWordWrap(True)
        self._warnings_label.setVisible(False)
        self._warnings_label.setProperty("role", "warning")
        outer.addWidget(self._warnings_label)
        return container

    # ------------------------------------------------------------------ #
    # style tab updates
    # ------------------------------------------------------------------ #
    def show_profile(self, profile: StyleProfile) -> None:
        """Refresh all detected-field rows + the preset table."""
        self._profile = profile
        self._no_text_label.setVisible(not profile.has_text_layer)

        field = profile.font_family
        self._rows["font_family"].set_field(
            str(field.value), field.confidence, field.origin
        )
        field = profile.weight
        weight = int(field.value)
        weight_text = f"{weight} ({'bold' if weight >= 600 else 'regular'})"
        self._rows["weight"].set_field(
            weight_text, field.confidence, field.origin
        )
        field = profile.italic
        self._rows["italic"].set_field(
            self.tr("Yes") if field.value else self.tr("No"),
            field.confidence,
            field.origin,
        )
        field = profile.size
        self._rows["size"].set_field(
            f"{float(field.value):.1f} pt", field.confidence, field.origin
        )
        field = profile.line_height
        self._rows["line_height"].set_field(
            f"{float(field.value):.1f} pt", field.confidence, field.origin
        )
        field = profile.color_srgb
        red, green, blue = (int(c) for c in field.value)
        hex_color = f"#{red:02x}{green:02x}{blue:02x}"
        self._rows["color_srgb"].set_field(
            f"{hex_color}  rgb({red},{green},{blue})",
            field.confidence,
            field.origin,
        )
        field = profile.alignment
        self._rows["alignment"].set_field(
            str(field.value), field.confidence, field.origin
        )
        field = profile.decorations
        decorations = ", ".join(str(d) for d in field.value) or "-"
        self._rows["decorations"].set_field(
            decorations, field.confidence, field.origin
        )

        for row_index, name in enumerate(_PRESETS):
            preset = profile.preset(name)
            if preset is None:
                continue
            status = self.tr("detected") if preset.found else self.tr("default")
            family = preset.font_family
            size = f"{preset.size:.1f} pt"
            align = preset.alignment
            conf = f"{round(preset.confidence * 100)}%"
            style_bits = []
            if preset.weight >= 600:
                style_bits.append(self.tr("bold"))
            if preset.italic:
                style_bits.append(self.tr("italic"))
            if style_bits:
                family = f"{family} {' '.join(style_bits)}"
            for column, text in ((1, status), (2, family), (3, size), (4, align), (5, conf)):
                item = self._presets_table.item(row_index, column)
                if item is None:
                    item = QTableWidgetItem(text)
                    self._presets_table.setItem(row_index, column, item)
                else:
                    item.setText(text)
        self._presets_table.resizeColumnsToContents()

    def show_coverage(
        self,
        page_coverage: PageCoverage | None,
        doc_coverage: DocumentCoverage | None,
    ) -> None:
        self._page_coverage = page_coverage
        self._doc_coverage = doc_coverage
        parts: list[str] = []
        if page_coverage is not None:
            head = (
                f"{self.tr('Page')}: {page_coverage.total_chars} {self.tr('chars')}"
            )
            if page_coverage.missing_chars == 0:
                head += f" · {self.tr('all glyphs covered')}"
            else:
                head += f" · {page_coverage.missing_chars} {self.tr('missing')}"
            parts.append(head)
            for item in page_coverage.missing[:8]:
                parts.append(
                    f"  \u2022 {item.char} {item.codepoint} \u00d7{item.count} "
                    f"\u2014 {item.font}"
                )
            if len(page_coverage.missing) > 8:
                parts.append(f"  \u2022 \u2026 {len(page_coverage.missing) - 8} more")
        if doc_coverage is not None:
            head = f"{self.tr('Document')}: {doc_coverage.total_chars} {self.tr('chars')}"
            if doc_coverage.ok:
                head += f" · {self.tr('all glyphs covered')}"
            else:
                head += (
                    f" · {doc_coverage.missing_chars} {self.tr('missing')} "
                    f"in {len({p for m in doc_coverage.missing for p in m.page_indices})} "
                    f"{self.tr('page(s)')}"
                )
            parts.append(head)
        if not parts:
            self._coverage_label.setText(self.tr("No coverage data yet."))
        else:
            self._coverage_label.setText("\n".join(parts))

    def show_perf(self, stats: dict) -> None:
        self._perf = stats
        totals = stats.get("totals", {})
        lines = [
            f"{self.tr('model')} {totals.get('model_ms', 0.0):.1f} ms · "
            f"{self.tr('detect')} {totals.get('detect_ms', 0.0):.1f} ms · "
            f"{self.tr('render')} {totals.get('render_ms', 0.0):.1f} ms"
        ]
        if stats.get("pages_per_second"):
            lines.append(
                f"{stats.get('warmed_pages', 0)}/{stats.get('page_count', 0)} "
                f"{self.tr('pages')} · {stats['pages_per_second']:.1f} {self.tr('pages/s')}"
            )
        self._perf_label.setText("  \u00b7  ".join(lines) if len(lines) == 1 else "\n".join(lines))

    def set_warm_progress(self, done: int, total: int) -> None:
        if total <= 0 or done >= total:
            self._progress_label.setVisible(False)
            return
        self._progress_label.setText(
            self.tr("Analyzing pages\u2026 {done}/{total}").format(
                done=done, total=total
            )
        )
        self._progress_label.setVisible(True)

    def clear_style(self) -> None:
        self._profile = None
        self._page_coverage = None
        self._doc_coverage = None
        self._perf = {}
        self._no_text_label.setVisible(False)
        self._progress_label.setVisible(False)
        self._coverage_label.setText(self.tr("No coverage data yet."))
        self._perf_label.setText("-")
        for row in self._rows.values():
            row.set_field("-", 0.0, "inherited")
        for row_index in range(len(_PRESETS)):
            for column in range(1, 6):
                item = self._presets_table.item(row_index, column)
                if item is not None:
                    item.setText("")

    def _copy_json_to_clipboard(self) -> None:
        from PySide6.QtWidgets import QApplication

        payload: dict = {}
        if self._profile is not None:
            payload["style"] = self._profile.to_dict()
        if self._page_coverage is not None:
            payload["coverage_page"] = self._page_coverage.to_dict()
        if self._doc_coverage is not None:
            payload["coverage_document"] = self._doc_coverage.to_dict()
        if self._perf:
            payload["perf"] = self._perf
        QApplication.clipboard().setText(
            json.dumps(payload, indent=2, ensure_ascii=False)
        )

    # ------------------------------------------------------------------ #
    # document tab updates
    # ------------------------------------------------------------------ #
    def set_document(self, document: PdfSession | None) -> None:
        """Refresh the document tab for ``document`` (or clear it)."""
        self._doc = document
        self.clear_style()
        while self._doc_layout.rowCount():
            self._doc_layout.removeRow(0)
        if document is None:
            self._warnings_label.setVisible(False)
            return

        meta = document.metadata()
        rows: list[tuple[str, str]] = [
            (self.tr("File"), document.display_name),
            (self.tr("Pages"), str(document.page_count)),
        ]
        if meta.get("Title"):
            rows.append((self.tr("Title"), meta["Title"]))
        if meta.get("Author"):
            rows.append((self.tr("Author"), meta["Author"]))
        if meta.get("Producer"):
            rows.append((self.tr("Producer"), meta["Producer"]))
        rows.append(
            (self.tr("Encryption"), self.tr("Yes") if document.is_encrypted else self.tr("No"))
        )
        rows.append(
            (self.tr("Digital signature"), self.tr("Yes") if document.is_signed else self.tr("No"))
        )
        for label, value in rows:
            name = QLabel(label)
            name.setProperty("role", "muted")
            field = QLabel(value)
            field.setWordWrap(True)
            field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._doc_layout.addRow(name, field)

        warnings = document.pdf_warnings()
        if warnings:
            lines = [f"\u2022 {w}" for w in warnings]
            self._warnings_label.setText("\n".join(lines))
            self._warnings_label.setVisible(True)
        else:
            self._warnings_label.setVisible(False)

    def set_auto_match(self, enabled: bool) -> None:
        self._auto_match.blockSignals(True)
        self._auto_match.setChecked(enabled)
        self._auto_match.blockSignals(False)

    def debug_overlay_enabled(self) -> bool:
        return self._debug_check.isChecked()

    def set_debug_overlay(self, enabled: bool) -> None:
        self._debug_check.blockSignals(True)
        self._debug_check.setChecked(enabled)
        self._debug_check.blockSignals(False)
