"""PdfSession: open/save/verify with pikepdf as source of truth."""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pymupdf
import pytest

from offlinepdf.core.document import (
    CorruptDocumentError,
    DocumentError,
    PasswordRequiredError,
    PdfSession,
)


def _tiny_pdf_bytes() -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def _encrypted_pdf(tmp_path: Path) -> Path:
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    out = tmp_path / "locked.pdf"
    pdf.save(out, encryption=pikepdf.Encryption(user="secret", owner="owner", R=6))
    pdf.close()
    return out


def test_open_basic(corpus_dir):
    with PdfSession.open(corpus_dir / "basic.pdf") as session:
        assert session.page_count == 2
        assert session.display_name == "basic.pdf"
        assert session.is_encrypted is False
        assert session.is_dirty is False
        assert session.pdf_version.startswith("1.")
        assert isinstance(session.metadata(), dict)
        assert isinstance(session.pdf_warnings(), list)


def test_open_missing_file(tmp_path):
    with pytest.raises(DocumentError):
        PdfSession.open(tmp_path / "nope.pdf")


def test_open_corrupt_bytes():
    with pytest.raises(CorruptDocumentError):
        PdfSession.open_bytes(b"this is definitely not a pdf")


def test_tolerant_header_skip():
    data = b"JUNK" * 5 + _tiny_pdf_bytes()
    with PdfSession.open_bytes(data) as session:
        assert session.page_count == 1


def test_encrypted_roundtrip(tmp_path):
    locked = _encrypted_pdf(tmp_path)
    with pytest.raises(PasswordRequiredError):
        PdfSession.open(locked)
    with PdfSession.open(locked, password="secret") as session:
        assert session.is_encrypted is True
        assert session.page_count == 1


def test_save_as_requires_overwrite(corpus_dir, tmp_path):
    target = tmp_path / "out.pdf"
    target.write_bytes(b"SENTINEL")
    with PdfSession.open(corpus_dir / "basic.pdf") as session:
        with pytest.raises(DocumentError):
            session.save_as(target)
        assert target.read_bytes() == b"SENTINEL"  # non-destructive
        session.save_as(target, overwrite=True)
    assert target.read_bytes()[:4] == b"%PDF"


def test_save_verification_blocks_bad_target(corpus_dir, tmp_path, monkeypatch):
    """If verification fails, the existing target must be untouched."""
    target = tmp_path / "victim.pdf"
    target.write_bytes(b"ORIGINAL")
    with PdfSession.open(corpus_dir / "basic.pdf") as session:
        # Simulate a verification failure inside _verify_file.
        def boom(path):
            raise DocumentError("Verification failed: simulated")

        monkeypatch.setattr(session, "_verify_file", boom)
        with pytest.raises(DocumentError):
            session.save_as(target, overwrite=True)
    assert target.read_bytes() == b"ORIGINAL"


def test_save_then_reopen(corpus_dir, tmp_path):
    target = tmp_path / "copy.pdf"
    with PdfSession.open(corpus_dir / "basic.pdf") as session:
        session.mark_dirty()
        assert session.is_dirty is True
        session.save_as(target, overwrite=True)
        assert session.is_dirty is False
        assert session.verify_roundtrip() is True
    with PdfSession.open(target) as again:
        assert again.page_count == 2


def test_save_without_path_fails(corpus_dir):
    data = (corpus_dir / "basic.pdf").read_bytes()
    with PdfSession.open_bytes(data) as session, pytest.raises(DocumentError):
        session.save()


def test_refresh_view_keeps_page_count(corpus_dir):
    with PdfSession.open(corpus_dir / "basic.pdf") as session:
        before = session.page_count
        session.refresh_view()
        assert session.page_count == before
        assert session.render_pixmap(0, 0.5).startswith(b"\x89PNG")


def test_closed_session_refuses_use(corpus_dir):
    session = PdfSession.open(corpus_dir / "basic.pdf")
    session.close()
    session.close()  # idempotent
    with pytest.raises(DocumentError):
        _ = session.pdf
