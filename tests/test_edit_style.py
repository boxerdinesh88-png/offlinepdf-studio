"""Edited text keeps the original formatting (font, weight, size, colour, layout)."""

from __future__ import annotations

import io
import itertools

import pikepdf
import pymupdf
import pytest
from reportlab.lib.colors import CMYKColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from offlinepdf.core.document import PdfSession
from offlinepdf.core.edit_engine import EditEngine
from offlinepdf.core.font_program import present_codes, tounicode_codes
from offlinepdf.core.font_resolver import STRATEGY_REUSE, FontResolver
from offlinepdf.core.inspect_session import InspectSession

F = "assets/fonts"
_FACES = {
    "Arimo-Bold": f"{F}/LiberationSans/LiberationSans-Bold.ttf",
    "Arimo": f"{F}/LiberationSans/LiberationSans-Regular.ttf",
    "Tinos-Italic": f"{F}/LiberationSerif/LiberationSerif-Italic.ttf",
    "Tinos": f"{F}/LiberationSerif/LiberationSerif-Regular.ttf",
    "Carlito-BoldItalic": f"{F}/Carlito/Carlito-BoldItalic.ttf",
}
for _name, _path in _FACES.items():
    pdfmetrics.registerFont(TTFont(_name, _path))

PARAGRAPH = (
    "This paragraph has several lines of body text that",
    "wrap inside a fixed column so that the editor must",
    "keep the same width, leading and font when reflowing.",
)


def _styled_pdf() -> bytes:
    """Bold heading, colour, italic, mixed-style line, CMYK, rotation, paragraph."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    c.setFont("Arimo-Bold", 24)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(50, 780, "Annual Report Heading")
    c.setFont("Arimo", 12)
    c.setFillColorRGB(0.8, 0.1, 0.1)
    c.drawString(50, 740, "Red warning text here")
    c.setFont("Tinos-Italic", 13)
    c.setFillColorRGB(0, 0, 0.6)
    c.drawString(50, 710, "An italic blue remark")
    t = c.beginText(50, 680)
    t.setFont("Arimo", 11)
    t.setFillColorRGB(0, 0, 0)
    t.textOut("Total due: ")
    t.setFont("Arimo-Bold", 11)
    t.setFillColorRGB(0.0, 0.5, 0.0)
    t.textOut("1,250 INR")
    t.setFont("Arimo", 11)
    t.setFillColorRGB(0, 0, 0)
    t.textOut(" by Friday")
    c.drawText(t)
    c.setFont("Carlito-BoldItalic", 14)
    c.setFillColor(CMYKColor(0, 0.9, 0.9, 0.1))
    c.drawString(50, 650, "CMYK print colour line")
    c.saveState()
    c.translate(520, 400)
    c.rotate(90)
    c.setFont("Arimo", 12)
    c.setFillColorRGB(0.2, 0.2, 0.2)
    c.drawString(0, 0, "Rotated margin note")
    c.restoreState()
    c.setFont("Tinos", 11)
    c.setFillColorRGB(0.15, 0.15, 0.15)
    for i, line in enumerate(PARAGRAPH):
        c.drawString(50, 600 - 14 * i, line)
    c.showPage()
    c.save()
    return buf.getvalue()


def _true_subset_pdf() -> bytes:
    """Identity-H subsets holding only the used glyphs, named like a Word export."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="AB", fontfile=f"{F}/LiberationSans/LiberationSans-Bold.ttf")
    page.insert_font(fontname="TI", fontfile=f"{F}/LiberationSerif/LiberationSerif-Italic.ttf")
    page.insert_text((50, 100), "Invoice Total", fontsize=16, fontname="AB", color=(0.2, 0.3, 0.7))
    page.insert_text((50, 140), "kindly remit", fontsize=11, fontname="TI", color=(0.5, 0.1, 0.1))
    doc.subset_fonts()
    pdf = pikepdf.open(io.BytesIO(doc.tobytes()))
    for _name, font in pdf.pages[0].Resources.Font.items():
        new = (
            "/ABCDEF+Arial-BoldMT" if "Bold" in str(font.BaseFont)
            else "/GHIJKL+TimesNewRomanPS-ItalicMT"
        )
        font.BaseFont = pikepdf.Name(new)
        for desc in font.DescendantFonts:
            desc.BaseFont = pikepdf.Name(new)
            desc.FontDescriptor.FontName = pikepdf.Name(new)
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _trace(page: pymupdf.Page, needle: str) -> dict:
    """Rendered style of the first text run containing ``needle``."""
    for span in page.get_texttrace():
        text = "".join(chr(c[0]) for c in span["chars"])
        if needle in text:
            return {
                "font": span["font"].split("+")[-1],
                "size": round(span["size"], 2),
                "colorspace": span["colorspace"],
                "color": tuple(round(v, 3) for v in span["color"]),
                "opacity": span["opacity"],
                "dir": tuple(round(v, 3) for v in span["dir"]),
            }
    raise AssertionError(f"{needle!r} not rendered")


def _edit(data: bytes, old: str, new: str) -> tuple[PdfSession, object]:
    session = PdfSession.open_bytes(data)
    inspect = InspectSession(session)
    span = next(s for s in inspect.model(0).spans if old in s.text)
    text = span.text.replace(old, new)
    return session, EditEngine(session, inspect, FontResolver()).edit_line(0, span.id, text)


# ---------------------------------------------------------------------- #
# real text: every style attribute survives, other runs untouched
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("old", "new", "probe"),
    [
        ("Annual Report Heading", "Annual Report Summary", "Summary"),  # bold heading
        ("Red warning text here", "Red warning text there", "there"),  # coloured RGB
        ("blue remark", "blue comment", "comment"),  # italic
        ("1,250 INR", "9,999 INR", "9,999"),  # one run inside a mixed-style line
        ("colour line", "colour row", "row"),  # CMYK + bold italic
        ("margin note", "margin text", "margin text"),  # rotated 90°
    ],
)
def test_edit_keeps_every_style_attribute(old, new, probe):
    data = _styled_pdf()
    before = _trace(pymupdf.open(stream=data)[0], old)
    others_before = [
        s for s in pymupdf.open(stream=data)[0].get_texttrace()
        if old not in "".join(chr(c[0]) for c in s["chars"])
    ]
    session, result = _edit(data, old, new)
    assert result.ok and result.badge == "original font reused (100%)"
    after = _trace(session.mupdf[0], probe)
    for key in ("font", "size", "colorspace", "color", "opacity", "dir"):
        assert after[key] == before[key], key
    others_after = [
        s for s in session.mupdf[0].get_texttrace()
        if probe not in "".join(chr(c[0]) for c in s["chars"])
    ]
    def sig(spans):
        return sorted(
            ("".join(chr(c[0]) for c in s["chars"]), s["font"], round(s["size"], 2),
             tuple(round(v, 3) for v in s["color"]))
            for s in spans
        )
    assert sig(others_after) == sig(others_before)  # untouched runs keep their styles


def _cmyk_fills(data: bytes) -> list[tuple[float, ...]]:
    pdf = pikepdf.open(io.BytesIO(data))
    return [
        tuple(round(float(v), 3) for v in inst.operands)
        for inst in pikepdf.parse_content_stream(pdf.pages[0])
        if str(inst.operator) == "k"
    ]


def test_cmyk_stays_cmyk_in_the_content_stream():
    data = _styled_pdf()
    before = _cmyk_fills(data)
    assert (0.0, 0.9, 0.9, 0.1) in before
    session, result = _edit(data, "colour line", "colour row")
    assert result.ok
    assert _cmyk_fills(session.to_bytes()) == before  # same DeviceCMYK fill, not converted


# ---------------------------------------------------------------------- #
# subset fonts: reuse when glyphs exist, same-family fallback otherwise
# ---------------------------------------------------------------------- #
def test_tounicode_coverage_only_counts_present_glyphs():
    pdf = pikepdf.open(io.BytesIO(_true_subset_pdf()))
    font = pdf.pages[0].Resources.Font["/AB"]
    assert len(tounicode_codes(font)) > 1000  # the whole original face is mapped
    assert "".join(sorted(chr(c) for c in present_codes(font))).strip() == "ITaceilnotv"


def test_subset_font_is_reused_when_it_has_the_glyphs():
    session, result = _edit(_true_subset_pdf(), "Invoice Total", "Total Invoice")
    assert result.ok and result.decision.strategy == STRATEGY_REUSE
    assert _trace(session.mupdf[0], "Total Invoice")["font"] == "Arial-BoldMT"


@pytest.mark.parametrize(
    ("old", "new", "probe", "face"),
    [
        ("Invoice Total", "Invoice Summary QZ", "Summary", "LiberationSans-Bold"),
        ("kindly remit", "kindly remit WXYZ now", "WXYZ", "LiberationSerif-Italic"),
    ],
)
def test_missing_glyphs_fall_back_in_family_with_warning(old, new, probe, face):
    data = _true_subset_pdf()
    before = _trace(pymupdf.open(stream=data)[0], old.split()[0])
    session, result = _edit(data, old, new)
    after = _trace(session.mupdf[0], probe)
    assert result.ok and result.decision.strategy != STRATEGY_REUSE  # UI shows the ⚠
    assert after["font"] == face  # metric-compatible, same weight/italic; never Helvetica
    assert (after["size"], after["color"]) == (before["size"], before["color"])


# ---------------------------------------------------------------------- #
# layout: longer text wraps inside the paragraph, never shrinks
# ---------------------------------------------------------------------- #
def test_longer_line_wraps_inside_the_paragraph_without_shrinking():
    session = PdfSession.open_bytes(_styled_pdf())
    inspect = InspectSession(session)
    engine = EditEngine(session, inspect, FontResolver())
    span = next(s for s in inspect.model(0).spans if s.text.startswith("wrap inside"))
    longer = (
        "wrap inside a fixed column so that the editor must, even when the "
        "sentence becomes much longer than before,"
    )
    route = engine.paragraph_for_line_edit(0, span.id, longer)
    assert route is not None
    result = engine.edit_block(0, *route)
    assert result.ok and result.lines_after == 4 and result.scale == 1.0
    words = [w for w in session.mupdf[0].get_text("words") if 230 < w[1] < 320]
    assert max(w[2] for w in words) <= 292.5  # original right edge
    baselines = sorted({round(w[3], 1) for w in words})
    assert all(abs((b - a) - 14.0) < 0.6 for a, b in itertools.pairwise(baselines))
    styles = {
        (s["font"].split("+")[-1], round(s["size"], 2), tuple(round(v, 3) for v in s["color"]))
        for s in session.mupdf[0].get_texttrace()
        if 230 < s["bbox"][1] < 320 and abs(s["dir"][1]) < 1e-6
    }
    assert styles == {("LiberationSerif", 11.0, (0.15, 0.15, 0.15))}


def test_short_edits_and_mixed_paragraphs_stay_line_edits():
    session = PdfSession.open_bytes(_styled_pdf())
    inspect = InspectSession(session)
    engine = EditEngine(session, inspect, FontResolver())
    span = next(s for s in inspect.model(0).spans if s.text.startswith("wrap inside"))
    assert engine.paragraph_for_line_edit(0, span.id, "wrap inside a column") is None
    heading = next(s for s in inspect.model(0).spans if "Annual" in s.text)
    assert engine.paragraph_for_line_edit(0, heading.id, "Annual " * 20) is None  # one line


# ---------------------------------------------------------------------- #
# WYSIWYG inline editor
# ---------------------------------------------------------------------- #
def test_inline_editor_shows_the_saved_style(qapp, tmp_path):
    from offlinepdf.config import Settings
    from offlinepdf.ui.main_window import MainWindow

    path = tmp_path / "styles.pdf"
    path.write_bytes(_styled_pdf())
    win = MainWindow(Settings(tmp_path / "settings.json"))
    assert win.open_path(path, restore_recovery=False)
    win.act_mode_edit.trigger()
    model = win.controller.inspect.model(0)
    for needle, weight_min, italic, color in (
        ("Annual Report", 700, False, "#1a1a1a"),
        ("An italic blue", 400, True, "#000099"),
        ("Red warning", 400, False, "#cc1a1a"),
    ):
        span = next(s for s in model.spans if needle in s.text)
        found = win.controller.hit_at(0, *model.to_view(*span.chars[1].origin))
        assert win._edit_controller.open(found)
        editor = win._edit_controller._editor
        font = editor._line.font()
        assert int(font.weight()) >= weight_min and font.italic() == italic
        assert abs(font.pixelSize() - round(span.size_mupdf * win.viewer.zoom)) <= 1
        assert color in editor._line.styleSheet()
        assert editor.text_size()[0] + 24 <= editor.width()  # whole text visible
        win._edit_controller.close_editor()
    win.close()


# ---------------------------------------------------------------------- #
# scanned pages: style from pixels
# ---------------------------------------------------------------------- #
def _have_rapidocr() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _have_rapidocr(), reason="rapidocr-onnxruntime missing")
def test_scanned_lines_edit_with_matching_style():
    from offlinepdf.core.ocr.pipeline import OcrOptions, recognize_page
    from offlinepdf.core.ocr.textlayer import make_page_editable

    rows = [
        ("LiberationSans/LiberationSans-Bold.ttf", 22, (0.1, 0.1, 0.1), "Quarterly Report Summary", "LiberationSans-Bold"),
        ("LiberationSans/LiberationSans-Regular.ttf", 12, (0, 0, 0), "Revenue grew by fourteen percent this year.", "LiberationSans"),
        ("LiberationSerif/LiberationSerif-Bold.ttf", 14, (0, 0, 0), "Board Approval and Budget", "LiberationSerif-Bold"),
        ("LiberationSerif/LiberationSerif-Italic.ttf", 12, (0, 0, 0), "Figures are provisional and unaudited.", "LiberationSerif-Italic"),
        ("LiberationSans/LiberationSans-Regular.ttf", 12, (0.75, 0.1, 0.1), "Payment overdue since March.", "LiberationSans"),
    ]
    src = pymupdf.open()
    page = src.new_page(width=595, height=420)
    y = 60.0
    for i, (path, size, color, text, _face) in enumerate(rows):
        page.insert_font(fontname=f"F{i}", fontfile=f"{F}/{path}")
        page.insert_text((50, y), text, fontsize=size, fontname=f"F{i}", color=color)
        y += size * 2.2
    scan = pymupdf.open()
    scan.new_page(width=595, height=420).insert_image(
        pymupdf.Rect(0, 0, 595, 420), stream=page.get_pixmap(dpi=200).tobytes("jpg", jpg_quality=85)
    )
    session = PdfSession.open_bytes(scan.tobytes())
    make_page_editable(session, 0, recognize_page(session.mupdf, 0, OcrOptions()))
    for _path, size, color, text, face in rows:
        inspect = InspectSession(session)
        span = next(s for s in inspect.model(0).spans if s.text.startswith(text[:10]))
        new = text.replace("e", "E", 1)
        assert EditEngine(session, inspect, FontResolver()).edit_line(0, span.id, new).ok
        got = _trace(session.mupdf[0], new[:10])
        assert got["font"] == face
        assert abs(got["size"] - size) <= 0.06 * size
        assert all(abs(a - b) <= 0.08 for a, b in zip(got["color"], color, strict=True))


def _upright(page: pymupdf.Page) -> bool:
    """Every character's baseline lies in the lower half of its box (not flipped)."""
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            if abs(line["dir"][1]) > 1e-6:  # rotated text: skip
                continue
            for span in line["spans"]:
                for char in span["chars"]:
                    _x0, y0, _x1, y1 = char["bbox"]
                    if char["c"].strip() and char["origin"][1] < (y0 + y1) / 2:
                        return False
    return True


def test_reflowed_text_is_upright():
    session = PdfSession.open_bytes(_styled_pdf())
    inspect = InspectSession(session)
    engine = EditEngine(session, inspect, FontResolver())
    span = next(s for s in inspect.model(0).spans if s.text.startswith("wrap inside"))
    longer = "wrap inside a fixed column so that the editor must, even when much longer,"
    assert engine.reflow_line(0, span.id, longer).ok
    assert _upright(session.mupdf[0])


def _mixed_paragraph_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    rows = [
        [("Tinos", (0.1, 0.1, 0.1), "The total amount of ")],
        [("Arimo-Bold", (0.0, 0.5, 0.0), "1,250 INR"), ("Tinos", (0.1, 0.1, 0.1), " is due by")],
        [("Tinos", (0.1, 0.1, 0.1), "Friday according to the agreed terms.")],
    ]
    for i, row in enumerate(rows):
        t = c.beginText(50, 700 - 14 * i)
        for font, color, text in row:
            t.setFont(font, 11)
            t.setFillColorRGB(*color)
            t.textOut(text)
        c.drawText(t)
    c.showPage()
    c.save()
    return buf.getvalue()


def test_mixed_style_paragraph_reflows_keeping_each_run():
    session = PdfSession.open_bytes(_mixed_paragraph_pdf())
    inspect = InspectSession(session)
    engine = EditEngine(session, inspect, FontResolver())
    span = next(s for s in inspect.model(0).spans if s.text.startswith("The total"))
    longer = "The complete outstanding total amount of the invoice, including all taxes, of "
    assert engine.paragraph_for_line_edit(0, span.id, longer) is not None
    result = engine.reflow_line(0, span.id, longer)
    assert result.ok and result.lines_after == 4 and result.scale == 1.0
    page = session.mupdf[0]
    green = _trace(page, "1,250 INR")
    assert green["font"] == "LiberationSans-Bold"
    assert green["color"] == pytest.approx((0.0, 0.5, 0.0), abs=0.01)
    body = _trace(page, "complete")
    assert (body["font"], body["size"]) == ("LiberationSerif", 11.0)
    assert body["color"] == pytest.approx((0.1, 0.1, 0.1), abs=0.01)
    words = [w for w in page.get_text("words") if 120 < w[1] < 200]
    assert max(w[2] for w in words) <= 245  # stays inside the paragraph's column
    assert _upright(page)
