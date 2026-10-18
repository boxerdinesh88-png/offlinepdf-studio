"""Offscreen main-window smoke: open, inspect, close without dialogs."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from offlinepdf.config import Settings
from offlinepdf.core.document import PdfSession
from offlinepdf.ui.main_window import MainWindow


@pytest.fixture
def window(qapp, tmp_path):
    settings = Settings(tmp_path / "settings.json")
    win = MainWindow(settings)
    yield win
    win.close()


def test_open_basic_pdf(window, corpus_dir):
    assert window.open_path(corpus_dir / "basic.pdf", restore_recovery=False) is True
    assert window.session is not None
    assert window.session.page_count == 2
    assert window.viewer.page_count() == 2
    assert window.viewer.zoom > 0
    assert window.windowTitle().startswith("basic.pdf")


def test_zoom_and_page_navigation(window, corpus_dir):
    window.open_path(corpus_dir / "basic.pdf", restore_recovery=False)
    window.viewer.fit_width()
    assert window.viewer.zoom > 0
    window.viewer.actual_size()
    assert abs(window.viewer.zoom - 1.0) < 1e-6
    window.viewer.zoom_in()
    assert window.viewer.zoom > 1.0
    window.viewer.scroll_to_page(1)
    assert window.viewer.current_page() in (0, 1)


def test_corrupt_open_reports_and_returns_false(window, tmp_path, monkeypatch):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    monkeypatch.setattr(
        QMessageBox, "critical", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    )
    assert window.open_path(bad, restore_recovery=False) is False
    assert window.session is None


def test_close_document_and_session_state(window, corpus_dir):
    window.open_path(corpus_dir / "basic.pdf", restore_recovery=False)
    session: PdfSession = window.session
    assert window.close_document() is True
    assert window.session is None
    assert session.is_closed is True


def test_no_document_actions_disabled(window):
    assert window.act_save.isEnabled() is False
    assert window.act_close_doc.isEnabled() is False
    assert window.act_ocr_page.isEnabled() is False
    window.close_document()


def test_ocr_action_requires_document(window, corpus_dir):
    assert window.act_ocr_page.isEnabled() is False
    window.open_path(corpus_dir / "basic.pdf", restore_recovery=False)
    assert window.act_ocr_page.isEnabled() is True
