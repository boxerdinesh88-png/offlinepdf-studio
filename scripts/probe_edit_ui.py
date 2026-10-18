"""Headless UI probe for Edit mode (Milestone M3).

Exercises the full inline-editor path offscreen: open, badge preview,
commit through the engine, status message, viewer/thumbnail refresh,
undo/redo actions and the close paths (Esc, zoom, click elsewhere).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from offlinepdf.core.text_model import HitResult
from offlinepdf.ui.main_window import MODE_EDIT, MainWindow


def _hit(win: MainWindow, page_index: int, x: float, y: float) -> HitResult | None:
    inspect = win.controller.inspect
    assert inspect is not None
    return inspect.model(page_index).hit_test_view(x, y)


def _some_hit(win: MainWindow, page_index: int) -> HitResult:
    """Any hit on real text, computed against the current page model."""
    inspect = win.controller.inspect
    assert inspect is not None
    model = inspect.model(page_index)
    span = next(s for s in model.spans if s.chars)
    cx = (span.bbox[0] + span.bbox[2]) / 2
    cy = (span.bbox[1] + span.bbox[3]) / 2
    vx, vy = model.to_view(cx, cy)
    found = model.hit_test_view(vx, vy)
    assert found is not None, "hit test failed on a real span"
    return found


def main() -> int:
    _app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    path = Path("tests/_corpus/basic.pdf")
    assert win.open_path(path), "open failed"

    win.act_mode_edit.trigger()
    assert win.mode == MODE_EDIT, f"mode is {win.mode}"

    inspect = win.controller.inspect
    assert inspect is not None
    model = inspect.model(0)
    span = next(s for s in model.spans if s.chars and len(s.text) > 5)
    cx = (span.bbox[0] + span.bbox[2]) / 2
    cy = (span.bbox[1] + span.bbox[3]) / 2
    vx, vy = model.to_view(cx, cy)
    hit = _hit(win, 0, vx, vy)
    assert hit is not None and hit.span.id == span.id
    original = span.text

    # --- open + live badge -------------------------------------------------
    win._on_edit_requested(hit)
    ctrl = win._edit_controller
    assert ctrl.is_open, "editor did not open"
    editor = ctrl._editor
    assert editor is not None
    badge = editor._badge.text()
    print(f"badge: {badge}")
    assert badge and "font" in badge, f"unexpected badge {badge!r}"
    assert editor._line.text() == original

    # --- preview on typing -------------------------------------------------
    typed = "Hi"
    editor._line.setText(typed)
    ctrl._preview(typed)
    badge2 = editor._badge.text()
    print(f"badge after typing: {badge2}")
    assert badge2, "badge cleared while typing"

    # --- commit ------------------------------------------------------------
    editor.committed.emit(typed)
    assert not ctrl.is_open, "editor stayed open after commit"
    status = win._status_message.text()
    print(f"status: {status}")
    assert status and "ms" in status, f"unexpected status {status!r}"
    assert win.session is not None and win.session.is_dirty
    text_after = win.session.mupdf.load_page(0).get_text()
    assert typed in text_after, "committed text not extractable"
    assert win.act_undo.isEnabled(), "undo action not enabled after edit"

    # --- undo / redo -------------------------------------------------------
    win.act_undo.trigger()
    text_undo = win.session.mupdf.load_page(0).get_text()
    assert typed not in text_undo, "undo did not revert the text"
    assert win.act_redo.isEnabled(), "redo action not enabled"
    win.act_redo.trigger()
    text_redo = win.session.mupdf.load_page(0).get_text()
    assert typed in text_redo, "redo did not restore the text"
    print("undo/redo ok")

    # --- close paths -------------------------------------------------------
    fresh = _some_hit(win, 0)
    win._on_edit_requested(fresh)
    assert ctrl.is_open
    ctrl._editor.cancelled.emit()
    assert not ctrl.is_open, "Esc did not close the editor"

    win._on_edit_requested(fresh)
    assert ctrl.is_open
    win.viewer.set_zoom(1.2)
    assert not ctrl.is_open, "zoom did not close the editor"

    win._on_edit_requested(fresh)
    assert ctrl.is_open
    win.viewer.spanClicked.emit(None, False)
    assert not ctrl.is_open, "click elsewhere did not close the editor"

    win._on_edit_requested(fresh)
    assert ctrl.is_open
    ctrl.close_editor()

    # --- refusal path keeps the app alive ----------------------------------
    assert win._editor_controller_open(None) is False

    print("probe_edit_ui: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
