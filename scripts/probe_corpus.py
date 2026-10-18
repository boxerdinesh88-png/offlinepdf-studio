"""Probe: text extraction and open behaviour of the M3 corpus files."""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pymupdf

from offlinepdf.core.document import PasswordRequiredError, PdfSession

CORPUS = Path("tests/_corpus")


def _text(name: str) -> str:
    doc = pymupdf.open(CORPUS / name)
    out = "\n".join(page.get_text() for page in doc)
    doc.close()
    return out


def main() -> int:
    for name in ("type3.pdf", "cid.pdf", "missing_glyph.pdf", "devanagari.pdf", "rtl.pdf"):
        print(f"--- {name}: {_text(name)!r}")
    print(f"--- outlined.pdf: {_text('outlined.pdf')!r}")

    with PdfSession.open(CORPUS / "basic.pdf") as session:
        toc = session.mupdf.get_toc()
        links = session.mupdf.load_page(0).get_links()
        print(f"--- basic toc={toc} links={len(links)} meta={session.metadata()}")
        print(f"    signed={session.is_signed}")

    with PdfSession.open(CORPUS / "signed.pdf") as session:
        print(f"--- signed.pdf is_signed={session.is_signed}")

    try:
        PdfSession.open(CORPUS / "encrypted.pdf")
    except PasswordRequiredError:
        print("--- encrypted.pdf: password required (expected)")
    else:
        raise AssertionError("encrypted.pdf opened without a password")

    with PdfSession.open(CORPUS / "encrypted.pdf", password="secret") as session:
        print(f"--- encrypted.pdf opened, pages={session.page_count}")

    for name in ("type3.pdf", "outlined.pdf"):
        pdf = pikepdf.open(CORPUS / name)
        print(f"--- {name} pikepdf ok, pages={len(pdf.pages)}")
        pdf.close()
    print("probe_corpus: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
