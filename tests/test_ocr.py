"""OCR: language pack discovery/verification and end-to-end recognition."""

from __future__ import annotations

import pytest

from offlinepdf.core.ocr import (
    OcrEngine,
    OcrWord,
    available_languages,
    ocr_page,
    require_language,
    verify_assets,
)
from offlinepdf.ui.ocr_controller import OcrController, line_ordered


def test_available_languages_include_en():
    languages = available_languages()
    codes = {pack.code for pack in languages}
    assert "en" in codes
    for pack in languages:
        assert pack.det_model.exists()
        assert pack.rec_model.exists()


def test_require_language_unknown_code():
    with pytest.raises(KeyError):
        require_language("not-a-language")


def test_verify_assets():
    ok, problems = verify_assets()
    assert ok, problems
    assert problems == []


@pytest.mark.slow
def test_recognize_text_on_page(corpus_dir):
    """Recognize English words on a rendered corpus page."""
    from offlinepdf.core.document import PdfSession

    with PdfSession.open(corpus_dir / "basic.pdf") as session:
        words = ocr_page(session, 0, language="en", scale=2.0)

    assert words, "expected at least one recognized word"
    joined = " ".join(word.text for word in words)
    assert "OfflinePDF" in joined or "Studio" in joined
    for word in words:
        assert len(word.box) == 4
        assert 0 <= word.confidence <= 1.0


@pytest.mark.slow
def test_ocr_page_boxes_in_point_space(corpus_dir):
    """Boxes from ocr_page are in PDF points, not rendered-pixel space."""
    from offlinepdf.core.document import PdfSession

    with PdfSession.open(corpus_dir / "basic.pdf") as session:
        page = session.mupdf.load_page(0)
        width_pt, height_pt = page.rect.width, page.rect.height
        words = ocr_page(session, 0, language="en", scale=2.0)

    assert words
    for word in words:
        for x, y in word.box:
            assert -1.0 <= x <= width_pt + 1.0
            assert -1.0 <= y <= height_pt + 1.0


@pytest.mark.slow
def test_engine_recognize_from_png(corpus_dir):
    """OcrEngine.recognize accepts PNG bytes directly."""
    from offlinepdf.core.document import PdfSession

    with PdfSession.open(corpus_dir / "basic.pdf") as session:
        png = session.render_pixmap(0, 2.0)
    engine = OcrEngine("en")
    words = engine.recognize(png)
    assert words


def test_line_ordered_sorts_top_to_bottom_left_to_right():
    words = [
        OcrWord(
            text="second-left",
            confidence=0.9,
            box=((100, 200), (150, 200), (150, 212), (100, 212)),
        ),
        OcrWord(
            text="first-left",
            confidence=0.9,
            box=((100, 100), (160, 100), (160, 112), (100, 112)),
        ),
        OcrWord(
            text="second-right",
            confidence=0.9,
            box=((300, 200), (390, 200), (390, 212), (300, 212)),
        ),
    ]
    assert line_ordered(words) == "first-left second-left second-right"
    assert line_ordered([]) == ""


def test_ocr_controller_launch_and_busy(qapp):
    controller = OcrController()
    assert controller.busy is False
    assert controller.run(None, 0, "en") is False  # no session -> rejected
