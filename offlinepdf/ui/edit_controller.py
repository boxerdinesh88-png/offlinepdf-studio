"""Edit-mode controller: inline editing over one span (Milestone M3).

Owns the :class:`~offlinepdf.core.edit_engine.EditEngine` for the open
document and hosts the :class:`~offlinepdf.ui.inline_editor.InlineEditor`
overlay on the page canvas.  Double-click opens the editor over the span;
the resolver preview keeps the status badge (same font / substitute /
synthetic + confidence) and the top-3 alternatives dropdown up to date while
typing.  Enter commits the edit through the engine (undo history included),
Esc cancels; zoom, page changes or clicks on other spans close the editor.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, Signal

from offlinepdf.core.document import PdfSession
from offlinepdf.core.edit_engine import EditEngine
from offlinepdf.core.font_program import program_bytes
from offlinepdf.core.font_resolver import STRATEGY_REUSE, FontResolver
from offlinepdf.core.text_model import HitResult, TextSpan
from offlinepdf.ui.fonts import pdf_font_family
from offlinepdf.ui.inline_editor import InlineEditor
from offlinepdf.ui.inspect_controller import InspectController
from offlinepdf.ui.viewer import Viewer

__all__ = ["EditController"]

logger = logging.getLogger(__name__)

_MIN_WIDTH = 160.0
_PAD = 3.0


class EditController(QObject):
    """Drives the inline editor and the edit engine for one document."""

    statusChanged = Signal(str)

    def __init__(
        self,
        viewer: Viewer,
        inspect_controller: InspectController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewer = viewer
        self._inspect_controller = inspect_controller
        self._session: PdfSession | None = None
        self._engine: EditEngine | None = None
        self._editor: InlineEditor | None = None
        self._target: tuple[int, int] | None = None  # (page index, span id)
        self._span: TextSpan | None = None
        #: let paragraph reflow narrow text (horizontal scale) to fit; off keeps
        #: the original size/scale and wraps onto more lines instead
        self.allow_shrink = False

        viewer.spanClicked.connect(self._on_span_clicked)
        viewer.zoomChanged.connect(self._on_zoom_changed)
        viewer.visiblePageChanged.connect(self._on_page_changed)

    # ------------------------------------------------------------------ #
    # document lifecycle
    # ------------------------------------------------------------------ #
    def set_session(self, session: PdfSession | None) -> None:
        self.close_editor()
        self._session = session
        inspect = self._inspect_controller.inspect
        if session is not None and inspect is not None:
            self._engine = EditEngine(session, inspect, FontResolver())
        else:
            self._engine = None

    @property
    def is_open(self) -> bool:
        return self._editor is not None and self._editor.isVisible()

    # ------------------------------------------------------------------ #
    # open / close
    # ------------------------------------------------------------------ #
    def open(self, hit: object) -> bool:
        """Open the inline editor over ``hit``'s span; False when unusable."""
        if self._engine is None or self._session is None:
            return False
        if not isinstance(hit, HitResult):
            return False
        self.close_editor()
        span = hit.span
        if span.id < 0 or not span.chars:
            return False
        page_index = hit.page_index
        self._target = (page_index, span.id)
        self._span = span

        editor = InlineEditor(self._viewer.canvas)
        editor.committed.connect(self._commit)
        editor.cancelled.connect(self.close_editor)
        editor.previewRequested.connect(self._preview)
        editor.infoRequested.connect(self.statusChanged.emit)
        self._editor = editor
        editor.set_text(span.text)
        self._layout_editor(page_index, span)
        editor.show()
        editor.raise_()
        editor.focus_editor()
        self._preview(span.text)
        return True

    def close_editor(self) -> None:
        if self._editor is not None:
            self._editor.hide()
            self._editor.deleteLater()
            self._editor = None
        self._target = None
        self._span = None

    # ------------------------------------------------------------------ #
    # preview / commit
    # ------------------------------------------------------------------ #
    def _preview(self, text: str) -> None:
        if self._engine is None or self._target is None or self._editor is None:
            return
        page_index, span_id = self._target
        decision, warning = self._engine.preview(page_index, span_id, text)
        self._editor.set_preview(decision, warning)
        if self._span is not None:
            zoom = self._viewer.canvas.zoom
            self._editor.apply_style(
                decision, self._span, zoom, **self._span_style(page_index)
            )
            self._layout_editor(page_index, self._span)  # fit the real font

    def _span_style(self, page_index: int) -> dict[str, Any]:
        """Original face, weight, italic, scale and spacing of the edited span."""
        span = self._span
        engine = self._engine
        if span is None or engine is None or not span.chars:
            return {}
        try:
            model = engine.inspect.model(page_index)
            event = model.shows[span.chars[0].event_idx]
        except (IndexError, Exception):
            return {}
        style: dict[str, Any] = {"stretch": float(event.tz), "letter_spacing": float(event.tc)}
        ref = event.font
        if ref is None:
            return style
        program = engine.inspect.programs.get(ref)
        style["weight"] = program.weight
        style["italic"] = program.italic
        font_dict = model.font_dicts.get(ref.objgen)
        packed = program_bytes(font_dict) if font_dict is not None else None
        style["original_family"] = pdf_font_family(ref.objgen, packed[0] if packed else None)
        return style

    def _commit(self, text: str) -> None:
        if self._engine is None or self._target is None:
            return
        page_index, span_id = self._target
        paragraph = self._engine.paragraph_for_line_edit(page_index, span_id, text)
        if paragraph is not None:
            # Longer text wraps inside the original paragraph width; every
            # other run keeps its own font, size and colour.
            mode = "auto_fit" if self.allow_shrink else "push_down"
            result = self._engine.reflow_line(page_index, span_id, text, overflow_mode=mode)
        else:
            result = self._engine.edit_line(page_index, span_id, text)
        if not result.ok:
            if self._editor is not None:
                self._editor.show_warning(result.warning)
            self.statusChanged.emit(result.warning or "The edit was refused.")
            return
        if result.noop:
            self.statusChanged.emit(f"{result.badge} · unchanged")
            self.close_editor()
            return
        parts = [result.badge]
        decision = result.decision
        if decision is not None and decision.strategy != STRATEGY_REUSE:
            parts = [
                f"⚠ Fallback font used: {decision.family or 'substitute'} instead of "
                f"{decision.requested or 'the original font'}"
            ]
        if result.path == "in-place":
            parts.append("text operators rewritten")
        elif result.path == "fallback":
            parts.append("fallback writer")
        if result.overflow:
            parts.append("wider than original (reflow in M4)")
        if result.warning:
            parts.append(result.warning.strip())
        parts.append(f"{result.duration_ms:.0f} ms")
        self.statusChanged.emit(" · ".join(parts))
        self.close_editor()

    # ------------------------------------------------------------------ #
    # placement
    # ------------------------------------------------------------------ #
    def _layout_editor(self, page_index: int, span: TextSpan) -> None:
        canvas = self._viewer.canvas
        geom = canvas.geom(page_index)
        if geom is None or self._editor is None:
            return
        model = None
        inspect = self._inspect_controller.inspect
        if inspect is not None:
            model = inspect.model(page_index)
        if model is None:
            return
        ax, ay = model.to_view(span.bbox[0], span.bbox[1])
        bx, by = model.to_view(span.bbox[2], span.bbox[3])
        x0, y0 = min(ax, bx), min(ay, by)
        x1, y1 = max(ax, bx), max(ay, by)
        zoom = canvas.zoom
        text_w, text_h = self._editor.text_size()
        width = max((x1 - x0) * zoom, _MIN_WIDTH, text_w + 24) + 2 * _PAD
        height = max(
            self._editor.sizeHint().height(),
            round((y1 - y0) * zoom) + 2 * _PAD,
            text_h + self._editor.badge_height() + 2 * _PAD + 6,
        )
        x = geom.x + x0 * zoom - _PAD
        y = geom.y + y0 * zoom - _PAD
        x = max(geom.x, min(x, geom.x + geom.width - width))
        y = max(geom.y, min(y, geom.y + geom.height - height))
        self._editor.setGeometry(
            round(x), round(y), round(width), round(height)
        )

    # ------------------------------------------------------------------ #
    # signal fan-in
    # ------------------------------------------------------------------ #
    def _on_span_clicked(self, hit: object, _double_click: bool) -> None:
        if not self.is_open:
            return
        current = self._target
        if current is None:
            self.close_editor()
            return
        if isinstance(hit, tuple) and len(hit) == 3:
            page_index = int(hit[0])
            found = self._inspect_controller.hit_at(
                page_index, float(hit[1]), float(hit[2])
            )
            if (
                found is not None
                and found.page_index == current[0]
                and found.span.id == current[1]
            ):
                return
        self.close_editor()

    def _on_zoom_changed(self, _zoom: float) -> None:
        self.close_editor()

    def _on_page_changed(self, page_index: int) -> None:
        if self._target is not None and page_index != self._target[0]:
            self.close_editor()
