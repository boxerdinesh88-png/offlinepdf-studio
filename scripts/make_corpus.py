"""Test corpus generation (ReportLab + hand-made edge cases).

Every builder returns the golden expectation dict for its PDF and writes the
goldens file next to the PDF.  Goldens are produced *by construction*: the
builder knows exactly what it drew (size, color, weight, alignment), so the
expected StyleProfile fields are recorded from the source parameters rather
than from detector output.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import pikepdf
from fontTools.pens.basePen import BasePen
from reportlab.lib.colors import black
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as RLTTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph

A4_W, A4_H = A4  # 595.27 x 841.89


def _asset(*parts: str) -> Path:
    return Path(__file__).resolve().parent.parent.joinpath("assets", "fonts", *parts)

# --- shared style constants (also referenced by goldens) ------------------

BODY_SIZE = 10.5
BODY_LEAD = 14.0
H1_SIZE, H1_Y = 24.0, 780.0
H2_SIZE, H2_Y = 18.0, 742.0
BODY_Y = 710.0
JUSTIFY_W = 420.0
CAPTION_SIZE, CAPTION_Y = 8.0, 180.0
FOOTER_SIZE, FOOTER_Y = 8.0, 36.0

H1_COLOR = (0.11, 0.21, 0.55)  # rgb floats
BODY_COLOR = (0.0, 0.0, 0.0)
CAPTION_COLOR = (0.45, 0.45, 0.45)
FOOTER_COLOR = (0.55, 0.55, 0.55)


def _rgb_tuple(rgb: tuple[float, float, float]) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(c * 255))) for c in rgb)  # type: ignore[return-value]


def _body_paragraph() -> Paragraph:
    style = ParagraphStyle(
        "body",
        fontName="Helvetica",
        fontSize=BODY_SIZE,
        leading=BODY_LEAD,
        alignment=TA_JUSTIFY,
        textColor=black,
    )
    text = (
        "OfflinePDF Studio edits existing PDF text in place while preserving "
        "the original typography. The style matching pipeline reads the font "
        "program, the graphics state and the layout geometry, then verifies "
        "the interpretation by re-rendering the span. This paragraph is fully "
        "justified so its left and right edges align on both margins."
    )
    return Paragraph(text, style)


def make_basic(path: Path) -> dict[str, Any]:
    """Standard styled document: headings, justified body, caption, footer."""
    can = rl_canvas.Canvas(str(path), pagesize=A4)
    can.setTitle("OfflinePDF Studio Test Corpus")
    can.setAuthor("OfflinePDF Studio")
    can.setSubject("M2 style detection and M3 editing regression corpus")
    can.setKeywords("offline, pdf, editing, corpus")

    # H1
    can.setFont("Helvetica-Bold", H1_SIZE)
    can.setFillColorRGB(*H1_COLOR)
    can.drawString(72, H1_Y, "OfflinePDF Studio")
    can.bookmarkPage("Heading")
    can.addOutlineEntry("Heading", "Heading", level=0)
    can.linkRect(
        "Go to heading",
        "Heading",
        (72, H1_Y - 4, 300, H1_Y + H1_SIZE + 4),
        relative=0,
    )

    # H2
    can.setFont("Helvetica-Bold", H2_SIZE)
    can.setFillColorRGB(0.1, 0.1, 0.1)
    can.drawString(72, H2_Y, "Style matching overview")

    # Justified body paragraph (Paragraph handles justification)
    para = _body_paragraph()
    _, ph = para.wrap(JUSTIFY_W, 500)
    para.drawOn(can, 72, BODY_Y - ph)

    # Left-aligned body continuation
    can.setFont("Helvetica", BODY_SIZE)
    can.setFillColorRGB(*BODY_COLOR)
    can.drawString(72, BODY_Y - ph - 20, "Left-aligned single line for alignment probing.")

    # Bullet list
    bullet_style = ParagraphStyle(
        "bullet",
        fontName="Helvetica",
        fontSize=BODY_SIZE,
        leading=BODY_LEAD,
        leftIndent=18,
        bulletIndent=4,
        textColor=black,
    )
    y = BODY_Y - ph - 44
    for item in ("First list item", "Second list item"):
        p = Paragraph(item, bullet_style)
        _, bh = p.wrap(JUSTIFY_W, 100)
        p.drawOn(can, 72, y - bh)
        y -= bh + 4

    # Caption (italic gray)
    can.setFont("Helvetica-Oblique", CAPTION_SIZE)
    can.setFillColorRGB(*CAPTION_COLOR)
    can.drawString(72, CAPTION_Y, "Figure 1: sample caption for detector tests.")

    # Footer (centered small gray)
    can.setFont("Helvetica", FOOTER_SIZE)
    can.setFillColorRGB(*FOOTER_COLOR)
    can.drawCentredString(A4_W / 2, FOOTER_Y, "OfflinePDF Studio - test corpus")

    # CMYK line (separate file probe target stays in basic for convenience)
    can.setFillColorCMYK(0.85, 0.10, 0.05, 0.0)  # red-ish in CMYK
    can.setFont("Helvetica-Bold", 14)
    can.drawString(72, 640, "CMYK heading line")
    can.setFillColorRGB(1, 1, 1)

    can.showPage()
    # Second page: rotated text at 30 degrees
    can.saveState()
    can.translate(160, 420)
    can.rotate(30)
    can.setFont("Helvetica-Bold", 20)
    can.setFillColorRGB(0.0, 0.35, 0.25)
    can.drawString(0, 0, "Rotated heading")
    can.restoreState()
    can.setFont("Helvetica", 12)
    can.setFillColorRGB(0, 0, 0)
    can.drawString(72, 100, "Page two bottom text.")
    can.bookmarkPage("Page 2")
    can.addOutlineEntry("Page 2", "Page 2", level=0)
    can.showPage()
    can.save()

    return {
        "file": path.name,
        "probes": {
            "h1": {
                "page": 0,
                "at": [90, H1_Y + 8],
                "expect": {
                    "size_pt": H1_SIZE,
                    "weight": 700,
                    "italic": False,
                    "family": "Helvetica",
                    "font_type": "Type1",
                    "is_embedded": False,
                    "fill_srgb": _rgb_tuple(H1_COLOR),
                    "alignment": "left",
                },
            },
            "h2": {
                "page": 0,
                "at": [90, H2_Y + 6],
                "expect": {
                    "size_pt": H2_SIZE,
                    "weight": 700,
                    "italic": False,
                    "family": "Helvetica",
                    "alignment": "left",
                    "fill_srgb": (26, 26, 26),
                },
            },
            "body_justified": {
                "page": 0,
                "at": [90, BODY_Y - 7],
                "expect": {
                    "size_pt": BODY_SIZE,
                    "weight": 400,
                    "italic": False,
                    "family": "Helvetica",
                    "fill_srgb": (0, 0, 0),
                    "alignment": "justify",
                },
            },
            "left_line": {
                "page": 0,
                "at": [90, BODY_Y - _body_height() - 15],
                "expect": {
                    "size_pt": BODY_SIZE,
                    "weight": 400,
                    "alignment": "left",
                    "fill_srgb": (0, 0, 0),
                },
            },
            "bullet": {
                "page": 0,
                "at": [90, BODY_Y - _body_height() - 40],
                "expect": {
                    "size_pt": BODY_SIZE,
                    "weight": 400,
                    "alignment": "left",
                },
            },
            "caption": {
                "page": 0,
                "at": [90, CAPTION_Y + 3],
                "expect": {
                    "size_pt": CAPTION_SIZE,
                    "weight": 400,
                    "italic": True,
                    "family": "Helvetica",
                    "fill_srgb": _rgb_tuple(CAPTION_COLOR),
                    "alignment": "left",
                },
            },
            "footer": {
                "page": 0,
                "at": [A4_W / 2, FOOTER_Y + 3],
                "expect": {
                    "size_pt": FOOTER_SIZE,
                    "weight": 400,
                    "fill_srgb": _rgb_tuple(FOOTER_COLOR),
                    "alignment": "center",
                },
            },
            "cmyk_line": {
                "page": 0,
                "at": [90, 645],
                "expect": {
                    "size_pt": 14.0,
                    "weight": 700,
                    "fill_space": "DeviceCMYK",
                    "fill_raw": [0.85, 0.10, 0.05, 0.0],
                    "alignment": "left",
                },
            },
            "rotated": {
                "page": 1,
                "at": "rotated-heading",
                "expect": {
                    "size_pt": 20.0,
                    "weight": 700,
                    "rotation_deg": 30.0,
                    "fill_srgb": (0, 89, 64),
                    "alignment": "left",
                },
            },
        },
        "presets_expect": {
            "Body": BODY_SIZE,
            "Heading 1": H1_SIZE,
            "Heading 2": H2_SIZE,
            "Caption": CAPTION_SIZE,
            "Footer": FOOTER_SIZE,
        },
    }


def _body_height() -> float:
    _, ph = _body_paragraph().wrap(JUSTIFY_W, 500)
    return ph


def make_perf(path: Path, pages: int = 500, target_mb: float = 50.0) -> dict[str, Any]:
    """500-page document padded with *distinct* noise JPEGs to ~target_mb.

    ReportLab embeds identical image streams only once, so every image must
    be unique; the pool is generated until its total size reaches the target.
    """
    import io

    import numpy as np
    from PIL import Image

    target_bytes = int(target_mb * 1024 * 1024)
    wanted = int(target_bytes * 1.08)  # headroom for compression variance
    pool: list[bytes] = []
    total = 0
    seed = 0
    while total < wanted and len(pool) < 800:
        rng = np.random.default_rng(seed)
        seed += 1
        arr = rng.integers(0, 256, size=(768, 768), dtype=np.uint8)
        arr = ((arr.astype(np.uint16) + np.roll(arr, 1, axis=1)) // 2).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(arr, mode="L").save(buf, format="JPEG", quality=62)
        data = buf.getvalue()
        pool.append(data)
        total += len(data)

    images_per_page = max(1, math.ceil(len(pool) / pages))
    slots = [560, 340, 120, -100]

    can = rl_canvas.Canvas(str(path), pagesize=A4)
    for i in range(pages):
        can.setFont("Helvetica", 12)
        can.setFillColorRGB(0, 0, 0)
        can.drawString(50, 812, f"Performance page {i + 1} of {pages}")
        for k in range(images_per_page):
            img = pool[(i * images_per_page + k) % len(pool)]
            y = slots[k % len(slots)]
            can.drawImage(
                ImageReader(io.BytesIO(img)),
                50,
                y,
                width=500,
                height=200,
                preserveAspectRatio=False,
            )
        can.showPage()
    can.save()

    size = path.stat().st_size
    return {
        "file": path.name,
        "pages": pages,
        "size_bytes": size,
        "images_per_page": images_per_page,
        "unique_images": len(pool),
        "target_bytes": target_bytes,
        "hit_target": size >= int(target_bytes * 0.8),
    }


# --------------------------------------------------------------------------- #
# M3 corpus: exotic fonts, scripts and safety cases
# --------------------------------------------------------------------------- #
def _tounicode_cmap(pairs: list[tuple[int, str]]) -> bytes:
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    for start in range(0, len(pairs), 100):
        block = pairs[start : start + 100]
        lines.append(f"{len(block)} beginbfchar")
        for code, unicode_text in block:
            encoded = unicode_text.encode("utf-16-be").hex().upper()
            lines.append(f"<{code:04X}> <{encoded}>")
        lines.append("endbfchar")
    lines += [
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    return "\n".join(lines).encode("latin-1")


def _draw_identity_text(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    ttf_path: Path,
    items: list[dict[str, Any]],
) -> None:
    """Draw text through an embedded TrueType Type0/Identity-H font.

    Each item is ``{"text", "x", "y", "size"}``.  Characters missing from the
    font cmap fall back to glyph 0 while ToUnicode still maps the code back to
    the intended character, which is exactly the "missing glyph" situation the
    M3 resolver has to survive.
    """
    from fontTools.ttLib import TTFont

    font = TTFont(str(ttf_path))
    cmap = font.getBestCmap()
    glyph_order = font.getGlyphOrder()
    units = font["head"].unitsPerEm
    hhea = font["hhea"]
    os2 = font.get("OS/2", None)
    cap_height = float(getattr(os2, "sCapHeight", 0) or hhea.ascent)
    widths = [
        round(font["hmtx"].metrics[name][0] / units * 1000) for name in glyph_order
    ]

    contents: list[str] = []
    code_to_char: dict[int, str] = {}
    for item in items:
        codes: list[int] = []
        for char in str(item["text"]):
            glyph_name = cmap.get(ord(char))
            gid = glyph_order.index(glyph_name) if glyph_name else 0
            code_to_char.setdefault(gid, char)
            codes.append(gid)
        hex_codes = "".join(f"{code:04X}" for code in codes)
        contents.append(
            f"BT /F1 {item['size']} Tf 1 0 0 1 {item['x']} {item['y']} Tm "
            f"<{hex_codes}> Tj ET"
        )

    head = font["head"]
    font_file = pdf.make_stream(ttf_path.read_bytes())
    descriptor = pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"),
        FontName=pikepdf.Name("/LibSans"),
        Flags=32,
        FontBBox=pikepdf.Array(
            [float(head.xMin), float(head.yMin), float(head.xMax), float(head.yMax)]
        ),
        ItalicAngle=0,
        Ascent=float(hhea.ascent),
        Descent=float(hhea.descent),
        CapHeight=cap_height,
        StemV=80,
        FontFile2=font_file,
    )
    cid_font = pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"),
        Subtype=pikepdf.Name("/CIDFontType2"),
        BaseFont=pikepdf.Name("/LibSans"),
        CIDSystemInfo=pikepdf.Dictionary(
            Registry=pikepdf.String(b"Adobe"),
            Ordering=pikepdf.String(b"Identity"),
            Supplement=0,
        ),
        FontDescriptor=descriptor,
        DW=500,
        W=pikepdf.Array([0, pikepdf.Array([float(w) for w in widths])]),
        CIDToGIDMap=pikepdf.Name("/Identity"),
    )
    to_unicode = pdf.make_stream(
        _tounicode_cmap(sorted(code_to_char.items()))
    )
    type0 = pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"),
        Subtype=pikepdf.Name("/Type0"),
        BaseFont=pikepdf.Name("/LibSans"),
        Encoding=pikepdf.Name("/Identity-H"),
        DescendantFonts=pikepdf.Array([cid_font]),
        ToUnicode=to_unicode,
    )
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=type0))
    page.Contents = pdf.make_stream("\n".join(contents).encode("latin-1"))
    font.close()


def _subset_ascii(src: Path, dst: Path) -> None:
    """Subset ``src`` to printable ASCII so accented glyphs go missing."""
    from fontTools.subset import Options, Subsetter
    from fontTools.ttLib import TTFont

    font = TTFont(str(src))
    options = Options()
    options.glyph_names = True
    options.notdef_outline = True
    subsetter = Subsetter(options=options)
    subsetter.populate(unicodes=list(range(0x20, 0x7F)))
    subsetter.subset(font)
    font.save(str(dst))
    font.close()


def _basic_source(path: Path) -> Path:
    source = path.parent / "basic.pdf"
    if not source.exists():
        make_basic(source)
    return source


def make_cid(path: Path) -> dict[str, Any]:
    """CID/Identity-H: embedded TrueType with 2-byte codes (M3 test d)."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(A4_W, A4_H))
    _draw_identity_text(
        pdf,
        page,
        _asset("LiberationSans", "LiberationSans-Regular.ttf"),
        [
            {"text": "CID Identity-H baseline text", "x": 72, "y": 720, "size": 16},
            {"text": "Second line for editing", "x": 72, "y": 690, "size": 12},
        ],
    )
    pdf.save(path)
    pdf.close()
    return {"file": path.name, "font": "LiberationSans (Type0/Identity-H)"}


def make_missing_glyph(path: Path) -> dict[str, Any]:
    """ASCII subset + accented text: the glyph-missing case (test c)."""
    subset_path = path.parent / "_subset_liberation_ascii.ttf"
    _subset_ascii(
        _asset("LiberationSans", "LiberationSans-Regular.ttf"), subset_path
    )
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(A4_W, A4_H))
    _draw_identity_text(
        pdf,
        page,
        subset_path,
        [
            {"text": "café latte", "x": 72, "y": 720, "size": 18},
            {"text": "plain ascii line", "x": 72, "y": 686, "size": 14},
        ],
    )
    pdf.save(path)
    pdf.close()
    subset_path.unlink(missing_ok=True)
    return {
        "file": path.name,
        "font": "LiberationSans ASCII subset",
        "missing_glyph": "é (U+00E9)",
    }


def make_type3(path: Path) -> dict[str, Any]:
    """Type3 (drawn) font: the engine must refuse with a warning (test e)."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(A4_W, A4_H))
    char_a = pdf.make_stream(b"600 0 0 0 600 700 d1 0 0 m 300 700 l 600 0 l h f")
    char_b = pdf.make_stream(
        b"600 0 0 0 600 700 d1 0 0 m 0 700 l 600 700 l 600 0 l h f"
    )
    char_procs = pikepdf.Dictionary()
    char_procs[pikepdf.Name("/A")] = char_a
    char_procs[pikepdf.Name("/B")] = char_b
    font = pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"),
        Subtype=pikepdf.Name("/Type3"),
        FontBBox=pikepdf.Array([0, 0, 600, 700]),
        FontMatrix=pikepdf.Array([0.001, 0, 0, 0.001, 0, 0]),
        CharProcs=char_procs,
        Encoding=pikepdf.Dictionary(
            Type=pikepdf.Name("/Encoding"),
            Differences=pikepdf.Array(
                [65, pikepdf.Name("/A"), 66, pikepdf.Name("/B")]
            ),
        ),
        FirstChar=65,
        LastChar=66,
        Widths=pikepdf.Array([600, 600]),
        Resources=pikepdf.Dictionary(),
    )
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.Contents = pdf.make_stream(
        b"BT /F1 36 Tf 1 0 0 1 72 700 Tm <414241> Tj ET"
    )
    pdf.save(path)
    pdf.close()
    return {"file": path.name, "font": "hand-made Type3", "text": "ABA"}


class _PDFPathPen(BasePen):
    """BasePen that emits PDF path operators (m/l/c/h)."""

    def __init__(self, glyph_set: Any) -> None:
        super().__init__(glyph_set)
        self.parts: list[str] = []

    def _moveTo(self, pt: tuple[float, float]) -> None:
        self.parts.append(f"{pt[0]:.2f} {pt[1]:.2f} m")

    def _lineTo(self, pt: tuple[float, float]) -> None:
        self.parts.append(f"{pt[0]:.2f} {pt[1]:.2f} l")

    def _curveToOne(
        self,
        p1: tuple[float, float],
        p2: tuple[float, float],
        p3: tuple[float, float],
    ) -> None:
        self.parts.append(
            f"{p1[0]:.2f} {p1[1]:.2f} {p2[0]:.2f} {p2[1]:.2f} "
            f"{p3[0]:.2f} {p3[1]:.2f} c"
        )

    def _qCurveToOne(
        self, p1: tuple[float, float], p2: tuple[float, float]
    ) -> None:
        p0 = self._getCurrentPoint()
        assert p0 is not None
        c1 = (p0[0] + (p1[0] - p0[0]) * 2 / 3, p0[1] + (p1[1] - p0[1]) * 2 / 3)
        c2 = (p2[0] + (p1[0] - p2[0]) * 2 / 3, p2[1] + (p1[1] - p2[1]) * 2 / 3)
        self._curveToOne(c1, c2, p2)

    def _closePath(self) -> None:
        self.parts.append("h")

    def _endPath(self) -> None:
        pass

    def commands(self) -> str:
        return " ".join(self.parts)


def make_outlined(path: Path) -> dict[str, Any]:
    """Glyph outlines drawn as plain paths - no font, no editable text (e)."""
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont

    ttf_path = _asset("LiberationSans", "LiberationSans-Bold.ttf")
    font = TTFont(str(ttf_path))
    glyph_set = font.getGlyphSet()
    units = font["head"].unitsPerEm
    scale = 36.0 / units
    commands: list[str] = []
    x = 72.0
    for glyph_name in ("O", "U", "T", "L", "I", "N", "E", "D"):
        emitter = _PDFPathPen(glyph_set)
        glyph_set[glyph_name].draw(
            TransformPen(emitter, (scale, 0, 0, scale, x, 700.0))
        )
        commands.append(emitter.commands())
        x += 300 * scale
    font.close()
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(A4_W, A4_H))
    page.Contents = pdf.make_stream(
        ("0 0 0 rg " + " ".join(commands) + " f").encode("ascii")
    )
    pdf.save(path)
    pdf.close()
    return {"file": path.name, "kind": "outlined paths", "text": "OUTLINED"}


def make_devanagari(path: Path) -> dict[str, Any]:
    """Mixed Latin + Devanagari line (M3 test d)."""
    pdfmetrics.registerFont(
        RLTTFont("NotoDeva", str(_asset("NotoSansDevanagari", "NotoSansDevanagari-Regular.ttf")))
    )
    can = rl_canvas.Canvas(str(path), pagesize=A4)
    can.setFont("NotoDeva", 20)
    can.setFillColorRGB(0, 0, 0)
    can.drawString(72, 720, "नमस्ते दुनिया Hello मिश्रित पाठ")
    can.setFont("NotoDeva", 14)
    can.drawString(72, 686, "दूसरी पंक्ति for editing")
    can.showPage()
    can.save()
    return {"file": path.name, "font": "NotoSansDevanagari", "scripts": "Deva+Latn"}


def make_rtl(path: Path) -> dict[str, Any]:
    """RTL Hebrew line drawn in visual order (M3 test d)."""
    pdfmetrics.registerFont(
        RLTTFont("NotoHebrew", str(_asset("NotoSansHebrew", "NotoSansHebrew-Regular.ttf")))
    )
    logical = "שלום עולם"
    visual = logical[::-1]  # reportlab has no bidi; draw visual order
    can = rl_canvas.Canvas(str(path), pagesize=A4)
    can.setFont("NotoHebrew", 20)
    can.setFillColorRGB(0, 0, 0)
    can.drawString(72, 720, visual)
    can.setFont("NotoHebrew", 14)
    can.drawString(72, 686, (logical + " Test")[::-1])
    can.showPage()
    can.save()
    return {"file": path.name, "font": "NotoSansHebrew", "direction": "rtl"}


def make_encrypted(path: Path) -> dict[str, Any]:
    """Password-protected copy of basic.pdf (M3 test g)."""
    pdf = pikepdf.open(_basic_source(path))
    pdf.save(
        path,
        encryption=pikepdf.Encryption(user="secret", owner="owner", R=6),
    )
    pdf.close()
    return {"file": path.name, "user_password": "secret", "encrypted": True}


def make_signed(path: Path) -> dict[str, Any]:
    """basic.pdf plus a (dummy) digital signature field (M3 test g)."""
    pdf = pikepdf.open(_basic_source(path))
    page = pdf.pages[0]
    signature = pikepdf.Dictionary(
        Type=pikepdf.Name("/Sig"),
        Filter=pikepdf.Name("/Adobe.PPKLite"),
        SubFilter=pikepdf.Name("/adbe.pkcs7.detached"),
        ByteRange=pikepdf.Array([0, 0, 0, 0]),
        Contents=pikepdf.String(b"\xff" * 4096),
    )
    field = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name("/Annot"),
            Subtype=pikepdf.Name("/Widget"),
            FT=pikepdf.Name("/Sig"),
            T=pikepdf.String("Signature1"),
            Rect=pikepdf.Array([0, 0, 0, 0]),
            P=page.obj,
            V=signature,
        )
    )
    page.Annots = pikepdf.Array([field])
    pdf.Root.AcroForm = pikepdf.Dictionary(
        SigFlags=3, Fields=pikepdf.Array([field])
    )
    pdf.save(path)
    pdf.close()
    return {"file": path.name, "signed": True, "sig_flags": 3}


def write_golden(golden: dict[str, Any], out_dir: Path) -> Path:
    """Write a golden JSON next to the corpus."""
    target = out_dir / "goldens" / (Path(golden["file"]).stem + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(golden, indent=2, sort_keys=True), encoding="utf-8")
    return target


def build(out_dir: Path, cases: list[str] | None = None) -> dict[str, Any]:
    """Build the corpus into ``out_dir``. Returns the manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    available = {
        "basic": make_basic,
        "perf": make_perf,
        "type3": make_type3,
        "outlined": make_outlined,
        "cid": make_cid,
        "missing_glyph": make_missing_glyph,
        "devanagari": make_devanagari,
        "rtl": make_rtl,
        "encrypted": make_encrypted,
        "signed": make_signed,
    }
    wanted = cases or list(available)
    manifest: dict[str, Any] = {"cases": {}}
    for name in wanted:
        builder = available[name]
        path = out_dir / f"{name}.pdf"
        info = builder(path)
        if "probes" in info:
            write_golden(info, out_dir)
        manifest["cases"][name] = info
    (out_dir / "corpus_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the OfflinePDF test corpus")
    parser.add_argument("--out", default="tests/_corpus", help="output directory")
    parser.add_argument("cases", nargs="*", help="subset of cases (default: all)")
    args = parser.parse_args(argv)
    manifest = build(Path(args.out), args.cases or None)
    for name, info in manifest["cases"].items():
        size = Path(args.out, info["file"]).stat().st_size
        print(f"  {name:12s} -> {info['file']} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

