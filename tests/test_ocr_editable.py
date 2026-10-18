"""Auto-Make Editable: detection, layout, OCR text layer, reveal-on-edit, UI queue."""

from __future__ import annotations

import io
import time

import numpy as np
import pymupdf
import pytest

from offlinepdf.core.document import PdfSession
from offlinepdf.core.edit_engine import EditEngine
from offlinepdf.core.font_resolver import FontResolver
from offlinepdf.core.inspect_session import InspectSession
from offlinepdf.core.ocr.cache import OcrCache
from offlinepdf.core.ocr.detect import classify_page
from offlinepdf.core.ocr.layout import OcrLine, Segment, _merge, build_lines, font_for_text
from offlinepdf.core.ocr.preprocess import IDENTITY, estimate_skew
from offlinepdf.core.ocr.textlayer import (
    apply_ocr_layer,
    make_page_editable,
    page_ocr_blocks,
    strip_unedited,
)

SERIF = "assets/fonts/LiberationSerif/LiberationSerif-Regular.ttf"
LINES = [
    (70, 24, "Quarterly Report 2026"),
    (120, 12, "Revenue grew by 14 percent compared to last year."),
    (140, 12, "The board approved the new budget on 12 March."),
]


def _have_rapidocr() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


needs_ocr = pytest.mark.skipif(not _have_rapidocr(), reason="rapidocr-onnxruntime missing")


def _scan_png(skew: float = 0.0, dpi: int = 200) -> bytes:
    from PIL import Image

    src = pymupdf.open()
    page = src.new_page(width=595, height=420)
    page.insert_font(fontname="F", fontfile=SERIF)
    for y, size, text in LINES:
        page.insert_text((50, y), text, fontsize=size, fontname="F")
    img = Image.open(io.BytesIO(page.get_pixmap(dpi=dpi).tobytes("png"))).convert("RGB")
    if skew:
        img = img.rotate(-skew, resample=Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _scanned_doc(pages: int = 1, skew: float = 0.0) -> bytes:
    doc = pymupdf.open()
    png = _scan_png(skew)
    for _ in range(pages):
        page = doc.new_page(width=595, height=420)
        page.insert_image(page.rect, stream=png)
    return doc.tobytes()


def _line(text: str, y: float, *, confidence: float = 0.95, x: float = 50.0) -> OcrLine:
    width = 7.0 * len(text)
    quad = ((x, y - 10), (x + width, y - 10), (x + width, y + 3), (x, y + 3))
    return OcrLine(
        text=text, confidence=confidence, quad=quad, origin=(x, y), angle=0.0, size=12.0,
        width=width, fg=(0.0, 0.0, 0.0), bg=(1.0, 1.0, 1.0), bg_uniform=True,
        font="LiberationSans", cover=quad,
    )


def _pixels(session: PdfSession) -> np.ndarray:
    pix = pymupdf.open(stream=session.to_bytes())[0].get_pixmap(dpi=72)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n).astype(int)


# ---------------------------------------------------------------------- #
# detection
# ---------------------------------------------------------------------- #
def test_detects_scanned_text_and_ocr_pages():
    doc = pymupdf.open(stream=_scanned_doc())
    assert classify_page(doc[0]).kind == "scanned"

    text = pymupdf.open()
    page = text.new_page()
    page.insert_text((50, 80), "Real text " * 20)
    assert classify_page(page).kind == "text"
    assert classify_page(text.new_page()).kind == "blank"


def test_mixed_document_is_classified_per_page():
    doc = pymupdf.open(stream=_scanned_doc())
    page = doc.new_page(width=595, height=420)
    page.insert_text((50, 80), "A normal page with selectable text.")
    kinds = [classify_page(doc[i]).kind for i in range(2)]
    assert kinds == ["scanned", "text"]


def test_little_text_over_big_image_counts_as_scanned():
    doc = pymupdf.open(stream=_scanned_doc())
    doc[0].insert_text((5, 10), "p.1", fontsize=6)  # e.g. a stamped page number
    assert classify_page(doc[0]).kind == "scanned"


def test_page_with_ocr_layer_is_not_recognised_again():
    session = PdfSession.open_bytes(_scanned_doc())
    apply_ocr_layer(session, 0, [_line("Hello world", 100)])
    assert classify_page(session.mupdf[0]).kind == "ocr"


# ---------------------------------------------------------------------- #
# preprocessing + layout
# ---------------------------------------------------------------------- #
def test_skew_estimate_recovers_rotation():
    from PIL import Image

    gray = np.asarray(Image.open(io.BytesIO(_scan_png(skew=3.0))).convert("L"))
    assert abs(estimate_skew(gray) - 3.0) <= 0.3
    straight = np.asarray(Image.open(io.BytesIO(_scan_png())).convert("L"))
    assert estimate_skew(straight) == 0.0


def test_merge_joins_split_line_segments():
    parts = [
        Segment("Typography quality: Hxg", 0.9, 0, 0, 230, 30),
        Segment("2024", 0.95, 250, 2, 300, 30),
        Segment("next line", 0.9, 0, 40, 90, 70),
    ]
    merged = _merge(parts)
    assert [m.text for m in merged] == ["Typography quality: Hxg 2024", "next line"]


def test_build_lines_size_baseline_and_colors():
    # A dark 12 px tall bar of "ink" on a light background at 1 px/pt.
    rgb = np.full((60, 200, 3), 240, np.uint8)
    rgb[22:34, 10:190:3] = (20, 30, 120)  # thin vertical strokes: ink is the minority
    seg = Segment("A1b", 0.9, 8, 20, 192, 36)  # too short to identify the face
    (line,) = build_lines([seg], rgb, IDENTITY, 1.0)
    # Sized from the 12 px ink band, not the padded 16 px detector box:
    # "A1b" (cap/ascender height, no descender) inks ~0.73 em in the sans.
    assert line.size == pytest.approx(12 / 0.73, rel=0.04)
    assert line.origin[1] == pytest.approx(34, abs=0.6)  # baseline on the ink bottom
    assert line.fg == pytest.approx((20 / 255, 30 / 255, 120 / 255), abs=0.05)
    assert line.bg == pytest.approx((240 / 255,) * 3, abs=0.02)
    assert line.cover is not None  # tight ink box for covering


def test_font_for_text_by_script():
    assert font_for_text("Hello") == "LiberationSans"
    assert font_for_text("भारत एक देश") == "NotoSansDevanagari"
    assert font_for_text("مرحبا") == "NotoSansArabic"


# ---------------------------------------------------------------------- #
# text layer + editing
# ---------------------------------------------------------------------- #
def test_layer_is_invisible_searchable_and_undoable():
    session = PdfSession.open_bytes(_scanned_doc())
    before = _pixels(session)
    written = make_page_editable(session, 0, [_line("Invoice number 4821", 100)])
    assert written == 1
    assert np.abs(_pixels(session) - before).max() == 0  # nothing visible changed
    assert session.mupdf[0].search_for("Invoice number 4821")
    assert not session.is_dirty
    session.history.undo()
    assert "Invoice" not in session.mupdf[0].get_text()


def test_editing_a_line_reveals_only_that_line():
    session = PdfSession.open_bytes(_scanned_doc())
    apply_ocr_layer(session, 0, [_line("First line text", 100), _line("Second line", 140)])
    inspect = InspectSession(session)
    engine = EditEngine(session, inspect, FontResolver())
    span = next(s for s in inspect.model(0).spans if "First" in s.text)
    result = engine.edit_line(0, span.id, "First line edited")
    assert result.ok, result.warning
    blocks = page_ocr_blocks(session.pdf, 0)
    assert [b.edited for b in blocks] == [True, False]
    assert "First line edited" in session.mupdf[0].get_text()
    # visible now: page differs from the untouched scan only around line 1
    session.history.undo()
    assert [b.edited for b in page_ocr_blocks(session.pdf, 0)] == [False, False]


def test_strip_unedited_keeps_edited_lines_only():
    session = PdfSession.open_bytes(_scanned_doc())
    apply_ocr_layer(session, 0, [_line("Keep me edited", 100), _line("Drop me", 140)])
    inspect = InspectSession(session)
    engine = EditEngine(session, inspect, FontResolver())
    span = next(s for s in inspect.model(0).spans if "Keep" in s.text)
    assert engine.edit_line(0, span.id, "Kept and edited").ok
    session.keep_ocr_layer = False
    exported = pymupdf.open(stream=session.export_bytes())[0].get_text()
    assert "Kept and edited" in exported and "Drop me" not in exported
    session.keep_ocr_layer = True
    assert "Drop me" in pymupdf.open(stream=session.export_bytes())[0].get_text()
    import pikepdf

    copy = pikepdf.open(io.BytesIO(session.to_bytes()))
    assert strip_unedited(copy) == 1


def test_cache_round_trip(tmp_path):
    cache = OcrCache(tmp_path)
    key = OcrCache.key(b"pixels", "en|300")
    assert cache.get(key) is None
    cache.put(key, [_line("Cached", 100)])
    (line,) = cache.get(key) or []
    assert line.text == "Cached" and line.cover == _line("Cached", 100).cover
    assert OcrCache.key(b"pixels", "devanagari|300") != key
    assert cache.clear() == 1


# ---------------------------------------------------------------------- #
# real recognition (needs the bundled models)
# ---------------------------------------------------------------------- #
@needs_ocr
@pytest.mark.parametrize("skew", [0.0, 3.0])
def test_recognise_edit_save_reopen(tmp_path, skew):
    from offlinepdf.core.ocr.pipeline import OcrOptions, recognize_page

    session = PdfSession.open_bytes(_scanned_doc(skew=skew))
    lines = recognize_page(session.mupdf, 0, OcrOptions(), cache=OcrCache(tmp_path / "c"))
    texts = [line.text for line in lines]
    assert [t for _, _, t in LINES] == texts
    assert all(abs(line.angle - skew) < 0.5 for line in lines)
    assert all(line.font == "LiberationSerif" for line in lines[1:])
    make_page_editable(session, 0, lines)
    inspect = InspectSession(session)
    engine = EditEngine(session, inspect, FontResolver())
    span = next(s for s in inspect.model(0).spans if "Revenue" in s.text)
    assert engine.edit_line(0, span.id, "Revenue grew by 21 percent compared to last year.").ok
    target = session.save_as(tmp_path / "out.pdf")
    reopened = pymupdf.open(target)
    assert reopened[0].search_for("21 percent") and reopened[0].search_for("budget")
    assert not reopened[0].search_for("14 percent")


@needs_ocr
def test_mixed_page_skips_lines_over_real_text():
    from offlinepdf.core.ocr.pipeline import OcrOptions, recognize_page

    doc = pymupdf.open(stream=_scanned_doc())
    doc[0].insert_text((50, 70), "Quarterly Report 2026", fontsize=24, render_mode=3)
    lines = recognize_page(doc, 0, OcrOptions())
    assert "Quarterly Report 2026" not in [line.text for line in lines]
    assert any("Revenue" in line.text for line in lines)


# ---------------------------------------------------------------------- #
# UI: auto-detect on open, background queue, double-click in Edit mode
# ---------------------------------------------------------------------- #
@needs_ocr
def test_window_makes_scanned_pdf_editable(qapp, tmp_path):
    from offlinepdf.config import Settings
    from offlinepdf.ui.main_window import MainWindow

    settings = Settings(tmp_path / "settings.json")
    win = MainWindow(settings)
    win._make_editable.cache = OcrCache(tmp_path / "cache")
    path = tmp_path / "scan.pdf"
    path.write_bytes(_scanned_doc(pages=2))
    assert win.open_path(path, restore_recovery=False)
    deadline = time.time() + 120
    while time.time() < deadline and not all(win._page_kind(p) == "ocr" for p in (0, 1)):
        qapp.processEvents()
        time.sleep(0.02)
    assert [win._page_kind(p) for p in (0, 1)] == ["ocr", "ocr"]
    assert "editable" in win._ocr_label.text().lower()
    assert not win.session.is_dirty
    win.close()


@needs_ocr
def test_cancel_stops_the_queue(qapp, tmp_path):
    from offlinepdf.ui.ocr_controller import MakeEditableController

    controller = MakeEditableController()
    controller.cache = OcrCache(tmp_path)
    session = PdfSession.open_bytes(_scanned_doc(pages=6))
    controller.set_session(session)
    done: list[int] = []
    finished: list[bool] = []
    controller.pageReady.connect(lambda page, _lines: done.append(page))
    controller.finished.connect(finished.append)
    controller.enqueue(list(range(6)))
    controller.cancel()
    deadline = time.time() + 60
    while time.time() < deadline and not finished:
        qapp.processEvents()
        time.sleep(0.02)
    assert finished == [True]
    assert len(done) <= 1


# ---------------------------------------------------------------------- #
# placement on real-world scan structures (regressions)
# ---------------------------------------------------------------------- #
def _structured_scan(kind: str) -> bytes:
    """One-page scan built the way various scanners/tools write them."""
    import pikepdf
    from PIL import Image

    img = Image.open(io.BytesIO(_scan_png(dpi=150))).convert("RGB")
    turn = {"rotated90": 90, "sideways": 90, "upside_down": 180}.get(kind, 0)
    if turn:
        img = img.rotate(turn, expand=True)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    pdf = pikepdf.new()
    size = (420, 595) if turn == 90 else (595, 420)
    pdf.add_blank_page(page_size=size)
    page = pdf.pages[0]
    stream = pdf.make_stream(
        buf.getvalue(), Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Image,
        Width=img.width, Height=img.height, ColorSpace=pikepdf.Name.DeviceRGB,
        BitsPerComponent=8, Filter=pikepdf.Name.DCTDecode,
    )
    page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=stream))
    w, h = size
    content = {
        "unbalanced_cm": f"{w} 0 0 {h} 0 0 cm /Im0 Do",
        "flipped_ctm": f"1 0 0 -1 0 {h} cm q {w} 0 0 -{h} 0 {h} cm /Im0 Do Q",
    }.get(kind, f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q")
    if kind == "rotated90":
        page.obj.Rotate = 90
    page.obj.Contents = pdf.make_stream(content.encode())
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


@needs_ocr
@pytest.mark.parametrize(
    "kind", ["unbalanced_cm", "flipped_ctm", "rotated90", "sideways", "upside_down"]
)
def test_layer_lands_on_the_visible_line(kind):
    from offlinepdf.core.ocr.pipeline import OcrOptions, recognize_page

    session = PdfSession.open_bytes(_structured_scan(kind))
    lines = recognize_page(session.mupdf, 0, OcrOptions())
    target = next(line for line in lines if "Revenue" in line.text)
    assert target.size == pytest.approx(12, abs=2.0)
    make_page_editable(session, 0, lines)
    inspect = InspectSession(session)
    xs = [p[0] for p in target.quad]
    ys = [p[1] for p in target.quad]
    hit = inspect.model(0).hit_test_view((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    assert hit is not None and "Revenue" in hit.span.text
    engine = EditEngine(session, inspect, FontResolver())
    assert engine.edit_line(0, hit.span.id, "Revenue grew by 21 percent compared to last year.").ok
    (found,) = session.mupdf[0].search_for("21 percent")
    found = found * session.mupdf[0].rotation_matrix
    assert min(xs) - 3 <= found.x0 and found.x1 <= max(xs) + 3
    assert min(ys) - 3 <= found.y0 and found.y1 <= max(ys) + 3


@needs_ocr
@pytest.mark.parametrize("header", [False, True])
def test_double_click_in_edit_mode_opens_editor(qapp, tmp_path, header):
    """The reported bug: double-clicking a scanned line showed nothing to edit."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from offlinepdf.config import Settings
    from offlinepdf.ui.main_window import MainWindow

    data = _structured_scan("unbalanced_cm")
    if header:  # a little real text on the scan: the page counts as "text"
        doc = pymupdf.open(stream=data)
        doc[0].insert_text((450, 400), "Page 1 of 1 - Confidential", fontsize=7)
        data = doc.tobytes()
    path = tmp_path / "scan.pdf"
    path.write_bytes(data)
    settings = Settings(tmp_path / "settings.json")
    settings.set("ocr_auto", not header)
    win = MainWindow(settings)
    win._make_editable.cache = OcrCache(tmp_path / "cache")
    win.resize(1200, 900)
    win.show()
    assert win.open_path(path, restore_recovery=False)
    win.act_mode_edit.trigger()
    canvas = win.viewer.canvas

    def wait(cond, timeout=120.0):
        end = time.time() + timeout
        while time.time() < end and not cond():
            qapp.processEvents()
            time.sleep(0.02)
        return cond()

    if not header:
        assert wait(lambda: win._page_kind(0) == "ocr")
    geom = canvas.geom(0)
    point = QPoint(round(geom.x + 120 * canvas.zoom), round(geom.y + 116 * canvas.zoom))
    QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    assert wait(lambda: win._edit_controller.is_open)
    assert "Revenue" in win._edit_controller._editor.text
    win._edit_controller.close_editor()
    win.close()


def test_rerecognise_replaces_unedited_lines_and_keeps_edits():
    from offlinepdf.core.ocr.textlayer import rerecognise_page

    session = PdfSession.open_bytes(_scanned_doc())
    # An old layer: one line the user edited, one misplaced line.
    apply_ocr_layer(session, 0, [_line("Edited by user", 100), _line("Misplaced", 300)])
    inspect = InspectSession(session)
    engine = EditEngine(session, inspect, FontResolver())
    span = next(s for s in inspect.model(0).spans if "Edited" in s.text)
    assert engine.edit_line(0, span.id, "Edited by the user").ok
    fresh = [_line("Edited by the user", 100), _line("Correct place", 140)]
    rerecognise_page(session, 0, fresh)
    blocks = page_ocr_blocks(session.pdf, 0)
    assert sorted(b.text for b in blocks) == ["Correct place", "Edited by user"]
    assert [b.edited for b in blocks if b.text == "Edited by user"] == [True]
    text = session.mupdf[0].get_text()
    assert "Misplaced" not in text and "Correct place" in text
    session.history.undo()
    assert "Misplaced" in session.mupdf[0].get_text()


# ---------------------------------------------------------------------- #
# charts / forms: ruling lines, vertical text (regressions from a real chart)
# ---------------------------------------------------------------------- #
def test_strip_rules_removes_lines_but_keeps_glyphs():
    from offlinepdf.core.ocr.layout import strip_rules

    mask = np.zeros((40, 300), bool)
    mask[38, :] = True  # axis / underline
    mask[:, 5] = True  # tick or cell border spanning the box
    for x in range(30, 260, 25):  # glyph-like blobs
        mask[12:30, x : x + 8] = True
    mask[8:32, 200:203] = True  # tall glyph stroke ("l", "/"), shorter than the box
    clean = strip_rules(mask)
    assert not clean[38, :].any() and not clean[:, 5].any()
    assert clean[12:30, 30:38].all() and clean[10:30, 200:203].all()


def _chart_scan() -> bytes:
    src = pymupdf.open()
    page = src.new_page(width=842, height=595)
    page.insert_font(fontname="S", fontfile="assets/fonts/LiberationSans/LiberationSans-Regular.ttf")
    page.insert_font(fontname="B", fontfile="assets/fonts/LiberationSans/LiberationSans-Bold.ttf")
    page.draw_rect(pymupdf.Rect(30, 25, 812, 95), color=(0, 0, 0), width=1.5)
    page.insert_text((300, 70), "PROJECT / WELL NAME", fontsize=22, fontname="B")
    axis_y = 520
    page.draw_line((120, axis_y), (780, axis_y), color=(0.3, 0.3, 0.3), width=1)
    page.draw_line((120, axis_y + 16), (780, axis_y + 16), color=(0.3, 0.3, 0.3), width=1)
    for i, label in enumerate(["12:41:30", "12:42:00", "12:42:30", "12:43:00", "12:43:30"]):
        x = 120 + i * 120
        page.draw_line((x, axis_y), (x, axis_y + 16), color=(0.3, 0.3, 0.3), width=1)
        page.insert_text((x + 4, axis_y + 11), label, fontsize=7, fontname="S", color=(0.25, 0.25, 0.3))
    page.insert_text((60, 360), "Channel3 (PSI)", fontsize=8, fontname="B", color=(0.9, 0.2, 0.2), rotate=90)
    scan = pymupdf.open()
    scan.new_page(width=842, height=595).insert_image(
        pymupdf.Rect(0, 0, 842, 595), stream=page.get_pixmap(dpi=150).tobytes("png")
    )
    return scan.tobytes()


@needs_ocr
def test_chart_labels_vertical_titles_and_lines(tmp_path):
    from offlinepdf.core.ocr.pipeline import OcrOptions, recognize_page

    session = PdfSession.open_bytes(_chart_scan())
    lines = recognize_page(session.mupdf, 0, OcrOptions())
    labels = [line for line in lines if line.text.startswith("12:4")]
    assert len(labels) >= 4
    assert all(abs(line.size - 7) <= 1.5 for line in labels)  # tick/axis lines ignored
    assert all(not line.bold and line.font == "LiberationSans" for line in labels)
    titles = [line for line in lines if "Channel" in line.text]
    assert titles and abs(abs(titles[0].angle) - 90) < 2  # vertical text, rotated line
    heading = next(line for line in lines if "PROJECT" in line.text)
    assert heading.bold and abs(heading.size - 22) <= 2

    make_page_editable(session, 0, lines)
    before = np.frombuffer(session.mupdf[0].get_pixmap(dpi=150).samples, np.uint8).copy()
    inspect = InspectSession(session)
    span = next(s for s in inspect.model(0).spans if s.text.startswith("12:43:00"))
    assert EditEngine(session, inspect, FontResolver()).edit_line(0, span.id, "12:43:05").ok
    pix = session.mupdf[0].get_pixmap(dpi=150)
    after = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)
    before = before.reshape(pix.h, pix.w, pix.n)
    scale = 150 / 72
    # the tick line left of the label and both axis lines next to it survive
    tick_x = round((120 + 3 * 120) * scale)
    rows = slice(round(522 * scale), round(534 * scale))
    assert np.array_equal(after[rows, tick_x - 1 : tick_x + 2], before[rows, tick_x - 1 : tick_x + 2])
    for y in (520, 536):
        row = round(y * scale)
        cols = slice(round(482 * scale), round(598 * scale))
        assert np.abs(after[row - 1 : row + 2, cols].astype(int) - before[row - 1 : row + 2, cols]).max() < 40


# ---------------------------------------------------------------------- #
# revert, searchable-only, edit-mode auto run, low-resolution warning
# ---------------------------------------------------------------------- #
def test_revert_page_removes_layer_and_edits_and_undoes():
    from offlinepdf.core.ocr.textlayer import revert_page

    session = PdfSession.open_bytes(_scanned_doc())
    original = _pixels(session)
    apply_ocr_layer(session, 0, [_line("First line text", 100), _line("Second", 140)])
    inspect = InspectSession(session)
    span = next(s for s in inspect.model(0).spans if "First" in s.text)
    assert EditEngine(session, inspect, FontResolver()).edit_line(0, span.id, "Edited").ok
    assert revert_page(session, 0) == 2
    assert session.mupdf[0].get_text().strip() == ""
    assert classify_page(session.mupdf[0]).kind == "scanned"
    assert np.abs(_pixels(session) - original).max() <= 2  # the scan again
    session.history.undo()
    assert "Edited" in session.mupdf[0].get_text()


def test_scan_dpi_reports_image_resolution():
    from offlinepdf.core.ocr.detect import scan_dpi

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=420)
    page.insert_image(page.rect, stream=_scan_png(dpi=100))
    assert abs(scan_dpi(doc[0]) - 100) < 2
    assert scan_dpi(pymupdf.open().new_page()) == 0.0


def _window(qapp, tmp_path, data: bytes, *, auto: bool):
    from offlinepdf.config import Settings
    from offlinepdf.ui.main_window import MainWindow

    settings = Settings(tmp_path / "settings.json")
    settings.set("ocr_auto", auto)
    win = MainWindow(settings)
    win._make_editable.cache = OcrCache(tmp_path / "cache")
    path = tmp_path / "scan.pdf"
    path.write_bytes(data)
    assert win.open_path(path, restore_recovery=False)
    return win


def _wait(qapp, cond, timeout=120.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        qapp.processEvents()
        time.sleep(0.02)
    return cond()


@needs_ocr
def test_entering_edit_mode_recognises_the_visible_scan(qapp, tmp_path):
    win = _window(qapp, tmp_path, _scanned_doc(pages=2), auto=False)
    assert _wait(qapp, lambda: not win._scan_timer.isActive())
    assert win._page_kind(0) == "scanned"  # auto-on-open is off
    win.act_mode_edit.trigger()
    assert _wait(qapp, lambda: win._page_kind(0) == "ocr")
    assert win._page_kind(1) == "scanned"  # only the visible page
    win.close()


@needs_ocr
def test_make_document_searchable_keeps_the_look(qapp, tmp_path):
    win = _window(qapp, tmp_path, _scanned_doc(pages=2), auto=False)
    assert _wait(qapp, lambda: not win._scan_timer.isActive())
    before = _pixels(win.session)
    win.act_make_searchable.trigger()
    assert _wait(qapp, lambda: all(win._page_kind(p) == "ocr" for p in (0, 1)))
    assert np.abs(_pixels(win.session) - before).max() == 0
    target = tmp_path / "searchable.pdf"
    win.session.save_as(target)
    assert pymupdf.open(target)[1].search_for("Revenue")
    win.close()


@needs_ocr
def test_low_resolution_scan_is_flagged(qapp, tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=420)
    page.insert_image(page.rect, stream=_scan_png(dpi=100))
    win = _window(qapp, tmp_path, doc.tobytes(), auto=False)
    assert _wait(qapp, lambda: not win._ocr_bar.isHidden())
    assert "Low-resolution scan" in win._ocr_label.text()
    win.close()


@needs_ocr
def test_table_cells_stay_separate_lines():
    from offlinepdf.core.ocr.pipeline import OcrOptions, recognize_page

    src = pymupdf.open()
    page = src.new_page(width=595, height=300)
    page.insert_font(fontname="S", fontfile="assets/fonts/LiberationSans/LiberationSans-Regular.ttf")
    cols = [50, 150, 250, 350, 450]
    rows = [("Item", "Qty", "Price", "Total"), ("Bolts", "120", "4.50", "540.00"),
            ("Nuts", "300", "1.25", "375.00")]
    for r, cells in enumerate(rows):
        y = 60 + r * 22
        page.draw_line((cols[0], y - 15), (cols[-1], y - 15), color=(0, 0, 0), width=0.8)
        for c, text in enumerate(cells):
            page.insert_text((cols[c] + 4, y), text, fontsize=11, fontname="S")
    for x in cols:
        page.draw_line((x, 45), (x, 60 + 2 * 22 + 7), color=(0, 0, 0), width=0.8)
    scan = pymupdf.open()
    scan.new_page(width=595, height=300).insert_image(
        pymupdf.Rect(0, 0, 595, 300), stream=page.get_pixmap(dpi=200).tobytes("png")
    )
    doc = pymupdf.open(stream=scan.tobytes())
    texts = [line.text for line in recognize_page(doc, 0, OcrOptions())]
    for cell in ("Item", "Qty", "Price", "Total", "Bolts", "540.00", "375.00"):
        assert cell in texts, (cell, texts)  # each cell its own line, never merged


def _phone_photo() -> bytes:
    """Page photographed at an angle on a desk: perspective, shading, blur, JPEG."""
    import cv2
    from PIL import Image

    src = pymupdf.open()
    page = src.new_page(width=595, height=842)
    page.insert_font(fontname="S", fontfile="assets/fonts/LiberationSans/LiberationSans-Regular.ttf")
    lines = ["Delivery Note 7731", "Ship to: Pune Warehouse, Gate 4",
             "Received 48 cartons in good condition.", "Driver signature required on arrival."]
    for i, text in enumerate(lines):
        page.insert_text((70, 120 + 50 * i), text, fontsize=22 if i == 0 else 14, fontname="S")
    paper = np.asarray(Image.open(io.BytesIO(page.get_pixmap(dpi=110).tobytes("png"))).convert("RGB"))
    ph, pw = paper.shape[:2]
    width, height = 1400, 1500
    quad = np.float32([[230, 120], [1180, 200], [1260, 1380], [130, 1300]])
    m = cv2.getPerspectiveTransform(np.float32([[0, 0], [pw, 0], [pw, ph], [0, ph]]), quad)
    img = np.full((height, width, 3), (60, 52, 45), np.uint8)
    inside = cv2.warpPerspective(np.full((ph, pw), 255, np.uint8), m, (width, height)) > 0
    img[inside] = cv2.warpPerspective(paper, m, (width, height))[inside]
    shade = np.linspace(1.05, 0.62, width)[None, :, None] * np.linspace(1.0, 0.85, height)[:, None, None]
    img = np.clip(img * shade + np.random.default_rng(3).normal(0, 6, img.shape), 0, 255)
    buf = io.BytesIO()
    Image.fromarray(cv2.GaussianBlur(img.astype(np.uint8), (3, 3), 0.8)).save(buf, "JPEG", quality=40)
    doc = pymupdf.open()
    sp = doc.new_page(width=width * 72 / 150, height=height * 72 / 150)
    sp.insert_image(sp.rect, stream=buf.getvalue())
    return doc.tobytes()


@needs_ocr
def test_phone_photo_perspective_and_clean_edit():
    from offlinepdf.core.ocr.pipeline import OcrOptions, recognize_page

    session = PdfSession.open_bytes(_phone_photo())
    lines = recognize_page(session.mupdf, 0, OcrOptions())
    target = next(line for line in lines if line.text.startswith("Received 48"))
    assert abs(target.angle - 4.8) < 0.6  # the sheet's real tilt, from perspective
    assert not any(line.bold for line in lines)  # blur is not boldness
    make_page_editable(session, 0, lines)
    inspect = InspectSession(session)
    span = next(s for s in inspect.model(0).spans if s.text.startswith("Received"))
    assert EditEngine(session, inspect, FontResolver()).edit_line(0, span.id, ".").ok
    # With the text edited away, the line's area is plain (shaded) paper again.
    xs = [p[0] for p in target.cover]
    ys = [p[1] for p in target.cover]
    clip = pymupdf.Rect(min(xs) + 8, min(ys), max(xs), max(ys))
    pix = session.mupdf[0].get_pixmap(dpi=150, clip=clip)
    gray = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].mean(axis=2)
    paper = np.percentile(gray, 90)
    assert (gray < paper - 45).mean() < 0.01  # no letters left behind


@needs_ocr
def test_uncertain_lines_get_per_word_confidence(qapp):
    from offlinepdf.core.ocr.pipeline import OcrOptions, recognize_page
    from offlinepdf.ui.ocr_review import OcrReviewOverlay
    from offlinepdf.ui.viewer import PageCanvas

    session = PdfSession.open_bytes(_chart_scan())
    lines = recognize_page(session.mupdf, 0, OcrOptions())
    sure = [line for line in lines if line.confidence >= 0.92]
    assert sure and all(not line.words for line in sure)  # no extra work when sure
    for line in lines:
        if line.words:
            assert [w for w, _c, _q in line.words] == line.text.split()
            assert all(0.0 <= c <= 1.0 for _w, c, _q in line.words)
    make_page_editable(session, 0, lines)
    overlay = OcrReviewOverlay(PageCanvas(), threshold=0.999)
    overlay.set_session(session)
    quads = overlay.uncertain_quads(0)
    assert quads and all(len(q) == 4 for q in quads)
