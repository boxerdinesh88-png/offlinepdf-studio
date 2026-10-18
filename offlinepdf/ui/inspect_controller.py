"""Inspect-mode controller: hit testing, style feeding and overlays.

Owns one :class:`~offlinepdf.core.inspect_session.InspectSession` per open
document and keeps the viewer overlays (hover, selection, debug geometry) and
the :class:`~offlinepdf.ui.inspector.StyleInspector` dock in sync.

Performance contract (M2): first click on a page builds the page model and
runs the detection pipeline; on the reference corpus this stays under the
50 ms click-to-style budget and the model build stays under 200 ms.  Cold
pages are warmed in the background so later clicks are cheap.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from offlinepdf.core.document import PdfSession
from offlinepdf.core.inspect_session import InspectSession
from offlinepdf.core.text_model import HitResult, PageModel
from offlinepdf.ui.inspector import StyleInspector
from offlinepdf.ui.viewer import Viewer

__all__ = ["InspectController"]

logger = logging.getLogger(__name__)

Hover = tuple[int, float, float]


def _view_rect(model: PageModel, rect: tuple[float, float, float, float]) -> tuple:
    ax, ay = model.to_view(rect[0], rect[1])
    bx, by = model.to_view(rect[2], rect[3])
    return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


def _view_geometry(model: PageModel) -> dict:
    """Convert the model-space debug geometry to view space for painting."""
    raw: dict[str, Any] = model.debug_geometry()
    out: dict = {"blocks": [], "spans": [], "chars": [], "baselines": []}
    for key in ("blocks", "spans", "chars"):
        out[key] = [_view_rect(model, rect) for rect in raw.get(key, [])]  # type: ignore[arg-type]
    for first, last in raw.get("baselines", []):
        fx, fy = model.to_view(first[0], first[1])
        lx, ly = model.to_view(last[0], last[1])
        out["baselines"].append(((fx, fy), (lx, ly)))
    return out


class InspectController(QObject):
    """Hit-testing bridge between the viewer and the inspector dock."""

    statusChanged = Signal(str)
    profileShown = Signal(object)  # StyleProfile
    editRequested = Signal(object)  # HitResult (double-click in Edit mode)

    def __init__(
        self, viewer: Viewer, inspector: StyleInspector, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._viewer = viewer
        self._inspector = inspector
        self._session: PdfSession | None = None
        self._inspect: InspectSession | None = None
        self._enabled = False
        self._debug = False
        self._selected: HitResult | None = None
        self._warm_timer = QTimer(self)
        self._warm_timer.setInterval(0)
        self._warm_timer.timeout.connect(self._warm_tick)

        viewer.hoverMoved.connect(self._on_hover)
        viewer.spanClicked.connect(self._on_click)
        inspector.debugOverlayToggled.connect(self.set_debug_overlay)

    # ------------------------------------------------------------------ #
    # document lifecycle
    # ------------------------------------------------------------------ #
    def set_session(self, session: PdfSession | None) -> None:
        self._session = session
        self._inspect = InspectSession(session) if session is not None else None
        self._selected = None
        self._viewer.canvas.clear_overlays()
        self._inspector.set_document(session)
        if session is not None:
            self._warm_timer.start()
        else:
            self._warm_timer.stop()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self._viewer.canvas.set_hover(None)
            self._viewer.canvas.clear_overlays()

    def set_debug_overlay(self, enabled: bool) -> None:
        self._debug = enabled
        if not enabled:
            self._viewer.canvas.set_debug_geometry(None, None)
        elif self._selected is not None:
            self._show_debug(self._selected.page_index)

    @property
    def inspect(self) -> InspectSession | None:
        return self._inspect

    @property
    def selected(self) -> HitResult | None:
        return self._selected

    # ------------------------------------------------------------------ #
    # background warming
    # ------------------------------------------------------------------ #
    def _warm_tick(self) -> None:
        if self._inspect is None:
            self._warm_timer.stop()
            return
        try:
            more = self._inspect.warm_next()
        except Exception:  # warming must never kill the UI
            logger.exception("page warm-up failed")
            self._warm_timer.stop()
            return
        done, total = self._inspect.warm_progress()
        self._inspector.set_warm_progress(done, total)
        if not more:
            self._warm_timer.stop()
            self._inspector.set_warm_progress(total, total)
            self._inspector.show_perf(self._inspect.perf_stats())

    # ------------------------------------------------------------------ #
    # hover / click
    # ------------------------------------------------------------------ #
    def _on_hover(self, hit: object) -> None:
        if not self._enabled or self._inspect is None or not isinstance(hit, tuple):
            return
        page_index, view_x, view_y = hit
        try:
            model = self._inspect.model(page_index)
            found = model.hit_test_view(view_x, view_y)
        except Exception:  # hover is best-effort
            return
        if found is None:
            self._viewer.canvas.set_hover(None)
            self.statusChanged.emit("")
            return
        rect = _view_rect(model, found.span.bbox)
        self._viewer.canvas.set_hover((page_index, view_x, view_y), rect)
        self.statusChanged.emit(f"{found.span.text[:40]!r}")

    def _on_click(self, hit: object, double_click: bool) -> None:
        if not self._enabled or self._inspect is None or not isinstance(hit, tuple):
            return
        page_index, view_x, view_y = hit
        try:
            model = self._inspect.model(page_index)
            found = model.hit_test_view(view_x, view_y)
        except Exception:  # clicks must never raise
            logger.exception("hit test failed")
            return
        if found is None:
            self._selected = None
            self._viewer.canvas.set_selection(None, None)
            return
        self._select(found)
        if double_click:
            self.editRequested.emit(found)

    def _select(self, found: HitResult) -> None:
        self._selected = found
        started = time.perf_counter()
        assert self._inspect is not None
        profile = self._inspect.profile(found.page_index)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        model = self._inspect.model(found.page_index)
        rect = _view_rect(model, found.span.bbox)
        self._viewer.canvas.set_selection(found.page_index, rect)
        self._inspector.show_profile(profile)
        self._inspector.show_coverage(
            self._inspect.coverage(found.page_index), None
        )
        self._inspector.show_perf(self._inspect.perf_stats())
        if self._debug:
            self._show_debug(found.page_index)
        self.statusChanged.emit(
            f"style in {elapsed_ms:.1f} ms · {found.span.font_mupdf} "
            f"{found.span.size_mupdf:.1f} pt"
        )
        self.profileShown.emit(profile)

    def _show_debug(self, page_index: int) -> None:
        if self._inspect is None:
            return
        try:
            geometry = _view_geometry(self._inspect.model(page_index))
        except Exception:  # debug drawing is optional
            return
        self._viewer.canvas.set_debug_geometry(page_index, geometry)

    # ------------------------------------------------------------------ #
    # helpers for Edit mode (M3)
    # ------------------------------------------------------------------ #
    def hit_at(self, page_index: int, view_x: float, view_y: float) -> HitResult | None:
        """Public hit test used by the inline editor."""
        if self._inspect is None:
            return None
        return self._inspect.model(page_index).hit_test_view(view_x, view_y)

    def invalidate(self) -> None:
        """Drop all cached state after the document changed."""
        if self._inspect is not None:
            self._inspect.invalidate()
        self._selected = None
        self._viewer.canvas.clear_overlays()
