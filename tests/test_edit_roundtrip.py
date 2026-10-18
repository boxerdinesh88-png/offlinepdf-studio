"""M3 round-trip tests: spec section 5, cases (a)-(g).

Every edit writes a before/after PNG into ``tests/output/`` so the results
can be inspected visually.  Corruption cases open fresh sessions per test;
files on disk are never modified.
"""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pymupdf
import pytest
from PIL import Image, ImageChops

from offlinepdf.core.document import PasswordRequiredError, PdfSession
from offlinepdf.core.edit_engine import EditEngine
from offlinepdf.core.font_resolver import STRATEGY_REUSE, FontResolver
from offlinepdf.core.inspect_session import InspectSession
from offlinepdf.core.text_model import TextSpan

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "tests" / "output"
_SCALE = 2.0


def _engine(session: PdfSession) -> tuple[InspectSession, EditEngine]:
    inspect = InspectSession(session)
    inspect.invalidate()
    return inspect, EditEngine(session, inspect, FontResolver())


def _span(model, fragment: str) -> TextSpan:
    for span in model.spans:
        if fragment in span.text:
            return span
    raise AssertionError(f"no span on page contains {fragment!r}")


def _save_png(label: str, which: str, data: bytes) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / f"{label}_{which}.png").write_bytes(data)
    return None


def _pixel_diff_ratio(before: bytes, after: bytes) -> float:
    """Fraction of channel samples that differ between two PNG buffers."""
    with Image.open(io.BytesIO(before)) as left, Image.open(
        io.BytesIO(after)
    ) as right:
        left_rgb = left.convert("RGB")
        right_rgb = right.convert("RGB")
        diff = ImageChops.difference(left_rgb, right_rgb)
        hist = diff.histogram()
        changed = 0
        for channel in range(3):
            base = channel * 256
            changed += sum(hist[base + 1 : base + 256])
        total = left_rgb.width * left_rgb.height * 3
    return changed / total


def _render(session: PdfSession, page: int) -> bytes:
    return session.render_pixmap(page, _SCALE)


# --------------------------------------------------------------------------- #
# (a) same text and (b) full reuse
# --------------------------------------------------------------------------- #
def test_a_same_text_pixel_diff(edit_corpus: Path) -> None:
    with PdfSession.open(edit_corpus / "basic.pdf") as session:
        inspect, engine = _engine(session)
        span = _span(inspect.model(0), "OfflinePDF Studio")
        before = _render(session, 0)
        result = engine.edit_line(0, span.id, span.text)
        after = _render(session, 0)
        _save_png("a_basic", "before", before)
        _save_png("a_basic", "after", after)
        assert result.ok is True
        assert result.noop is True
        assert _pixel_diff_ratio(before, after) <= 0.005


def test_b_original_font_reused(edit_corpus: Path) -> None:
    with PdfSession.open(edit_corpus / "basic.pdf") as session:
        inspect, engine = _engine(session)
        span = _span(inspect.model(0), "OfflinePDF Studio")
        before = _render(session, 0)
        result = engine.edit_line(0, span.id, "OfflinePDF Studio 2")
        after = _render(session, 0)
        _save_png("b_basic", "before", before)
        _save_png("b_basic", "after", after)
        assert result.ok is True
        assert result.noop is False
        assert result.decision is not None
        assert result.decision.strategy == STRATEGY_REUSE
        assert result.badge == "original font reused (100%)"
        assert result.path == "in-place"


# --------------------------------------------------------------------------- #
# (c) glyphs missing from a subset
# --------------------------------------------------------------------------- #
def test_c_missing_glyph_fallback(edit_corpus: Path) -> None:
    with PdfSession.open(edit_corpus / "missing_glyph.pdf") as session:
        inspect, engine = _engine(session)
        span = _span(inspect.model(0), "café latte")
        before_size = span.size_mupdf
        before_flags = span.flags
        before_color = span.color_int
        before = _render(session, 0)
        result = engine.edit_line(0, span.id, "Café Crème")
        after = _render(session, 0)
        _save_png("c_missing_glyph", "before", before)
        _save_png("c_missing_glyph", "after", after)
        assert result.ok is True, result.warning
        assert result.decision is not None
        assert result.decision.strategy != STRATEGY_REUSE
        assert result.decision.confidence < 1.0
        assert result.decision.badge and "100%" not in result.badge
        inspect.invalidate()
        span_after = _span(inspect.model(0), "Café Crème")
        assert abs(span_after.size_mupdf - before_size) < 0.05
        assert span_after.flags == before_flags
        assert span_after.color_int == before_color


# --------------------------------------------------------------------------- #
# (d) rotated, CMYK, CID/Identity-H, justified, mixed scripts, RTL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("name", "page", "fragment", "new_text"),
    [
        (
            "basic",
            0,
            "CMYK heading line",
            "CMYK heading line 2",
        ),
        (
            "basic",
            0,
            "OfflinePDF Studio edits existing",
            "OfflinePDF Studio edits EXISTING PDF text in place "
            "while preserving the original typography.",
        ),
        ("basic", 1, "Rotated heading", "Rotated heading 2"),
        ("cid", 0, "CID Identity-H baseline text", "CID Identity-H baseline text 2"),
        (
            "devanagari",
            0,
            "नमस्ते दुनिया",
            "नमस्ते दुनिया Hello Hello",
        ),
        ("rtl", 0, "שלום עולם", "שלום עולם סדרת מבחן"),
    ],
    ids=["cmyk", "justified", "rotated", "cid", "mixed-devanagari", "rtl"],
)
def test_d_special_layouts_roundtrip(
    edit_corpus: Path,
    name: str,
    page: int,
    fragment: str,
    new_text: str,
) -> None:
    with PdfSession.open(edit_corpus / f"{name}.pdf") as session:
        inspect, engine = _engine(session)
        span = _span(inspect.model(page), fragment)
        before = _render(session, page)
        result = engine.edit_line(page, span.id, new_text)
        after = _render(session, page)
        _save_png(f"d_{name}", "before", before)
        _save_png(f"d_{name}", "after", after)
        assert result.ok is True, result.warning
        doc = pymupdf.open(stream=session.to_bytes(), filetype="pdf")
        text = doc[page].get_text()
        doc.close()
        if name == "rtl":
            assert new_text in text or new_text[::-1] in text
        else:
            assert new_text in text


# --------------------------------------------------------------------------- #
# (e) Type3 and outlined text
# --------------------------------------------------------------------------- #
def test_e_type3_and_outlined_refused(edit_corpus: Path) -> None:
    with PdfSession.open(edit_corpus / "type3.pdf") as session:
        inspect, engine = _engine(session)
        span = _span(inspect.model(0), "ABA")
        reason = engine.can_edit(0, span.id)
        assert reason and "Type3" in reason
        result = engine.edit_line(0, span.id, span.text + "!")
        _save_png("e_type3", "before", _render(session, 0))
        _save_png("e_type3", "after", _render(session, 0))
        assert result.ok is False
        assert "Type3" in result.warning

    with PdfSession.open(edit_corpus / "outlined.pdf") as session:
        inspect, engine = _engine(session)
        assert not inspect.model(0).spans
        assert engine.can_edit(0, 0)
        result = engine.edit_line(0, 0, "text")
        _save_png("e_outlined", "before", _render(session, 0))
        _save_png("e_outlined", "after", _render(session, 0))
        assert result.ok is False
        assert result.warning


# --------------------------------------------------------------------------- #
# (f) output validation
# --------------------------------------------------------------------------- #
def test_f_output_validation(edit_corpus: Path) -> None:
    with PdfSession.open(edit_corpus / "basic.pdf") as session:
        inspect, engine = _engine(session)
        span = _span(inspect.model(0), "OfflinePDF Studio edits existing")
        new_text = span.text.replace("original", "ORIGINAL")
        before = _render(session, 0)
        result = engine.edit_line(0, span.id, new_text)
        after = _render(session, 0)
        _save_png("f_basic", "before", before)
        _save_png("f_basic", "after", after)
        assert result.ok is True
        assert session.verify_roundtrip()
        data = session.to_bytes()
        with pikepdf.open(io.BytesIO(data)) as pdf:
            assert len(pdf.pages) == session.page_count
        doc = pymupdf.open(stream=data, filetype="pdf")
        text = doc[0].get_text()
        assert new_text in text
        assert doc.get_toc() == [[1, "Heading", 1], [1, "Page 2", 2]]
        assert len(doc[0].get_links()) == 1
        meta = doc.metadata
        assert meta["title"] == "OfflinePDF Studio Test Corpus"
        assert meta["author"] == "OfflinePDF Studio"
        assert meta["subject"].startswith("M2 style detection")
        assert "offline, pdf, editing" in meta["keywords"]
        doc.close()


# --------------------------------------------------------------------------- #
# (g) encrypted and signed PDFs
# --------------------------------------------------------------------------- #
def test_g_encrypted_and_signed(edit_corpus: Path) -> None:
    with pytest.raises(PasswordRequiredError):
        PdfSession.open(edit_corpus / "encrypted.pdf")

    with PdfSession.open(
        edit_corpus / "encrypted.pdf", password="secret"
    ) as session:
        assert session.is_encrypted
        inspect, engine = _engine(session)
        span = _span(inspect.model(0), "OfflinePDF Studio")
        before = _render(session, 0)
        result = engine.edit_line(0, span.id, span.text + "!")
        after = _render(session, 0)
        _save_png("g_encrypted", "before", before)
        _save_png("g_encrypted", "after", after)
        assert result.ok is True
        assert "encrypted" in result.warning
        data = session.to_bytes()
        with pytest.raises(pikepdf.PasswordError):
            pikepdf.open(io.BytesIO(data))
        with pikepdf.open(io.BytesIO(data), password="secret") as pdf:
            assert pdf.is_encrypted

    with PdfSession.open(edit_corpus / "signed.pdf") as session:
        assert session.is_signed
        inspect, engine = _engine(session)
        span = _span(inspect.model(0), "OfflinePDF Studio")
        before = _render(session, 0)
        result = engine.edit_line(0, span.id, span.text + "!")
        after = _render(session, 0)
        _save_png("g_signed", "before", before)
        _save_png("g_signed", "after", after)
        assert result.ok is True
        assert "signature" in result.warning
