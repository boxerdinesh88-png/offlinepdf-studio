"""Performance budgets: 500-page / 50 MB document opens and renders in time."""

from __future__ import annotations

import time

import pytest

from offlinepdf.core.document import PdfSession

OPEN_RENDER_BUDGET_MS = 3000.0


@pytest.mark.slow
def test_perf_corpus_shape(perf_corpus):
    pdf = perf_corpus / "perf.pdf"
    size = pdf.stat().st_size
    assert size >= 40 * 1024 * 1024, f"perf corpus too small: {size:,} bytes"
    with PdfSession.open(pdf) as session:
        assert session.page_count == 500


@pytest.mark.slow
def test_open_and_render_within_budget(perf_corpus, capsys):
    pdf = perf_corpus / "perf.pdf"

    t0 = time.perf_counter()
    session = PdfSession.open(pdf)
    t1 = time.perf_counter()
    png = session.render_pixmap(0, 1.0)
    t2 = time.perf_counter()
    session.close()

    open_ms = (t1 - t0) * 1000
    render_ms = (t2 - t1) * 1000
    total = open_ms + render_ms
    with capsys.disabled():
        print(f"\nperf: open {open_ms:.0f} ms, render page 1 {render_ms:.0f} ms")
    assert png.startswith(b"\x89PNG")
    assert total < OPEN_RENDER_BUDGET_MS, (
        f"open+render took {total:.0f} ms (budget {OPEN_RENDER_BUDGET_MS:.0f} ms)"
    )
