"""Probe: EditEngine behaviour across the M3 corpus cases."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from offlinepdf.core.document import PasswordRequiredError, PdfSession
from offlinepdf.core.edit_engine import EditEngine
from offlinepdf.core.font_resolver import FontResolver
from offlinepdf.core.inspect_session import InspectSession

CORPUS = Path("tests/_corpus")
CASES = [
    "basic",
    "type3",
    "outlined",
    "cid",
    "missing_glyph",
    "devanagari",
    "rtl",
    "signed",
]


def _session(
    path: Path, password: str | None = None
) -> tuple[PdfSession, InspectSession, EditEngine]:
    session = PdfSession.open(path, password=password or "")
    inspect = InspectSession(session)
    inspect.invalidate()
    engine = EditEngine(session, inspect, FontResolver())
    return session, inspect, engine


def _text(session: PdfSession, page: int = 0) -> str:
    doc = pymupdf.open(stream=session.to_bytes(), filetype="pdf")
    got = doc[page].get_text()
    doc.close()
    return got


def _try(name: str) -> None:
    session, inspect, engine = _session(CORPUS / f"{name}.pdf")
    try:
        print(f"=== {name}: pages={session.page_count} signed={session.is_signed}")
        for page_index in range(min(session.page_count, 2)):
            model = inspect.model(page_index)
            if not model.spans:
                print(f"    page{page_index}: no spans")
                continue
            span = model.spans[0]
            reason = engine.can_edit(page_index, span.id)
            text = span.text
            if name == "missing_glyph" and page_index == 0:
                new_text = "Café Crème"
            elif name == "rtl" and page_index == 0:
                new_text = "שלום עולם סדרת מבחן"
            else:
                new_text = text.replace("a", "X") if "a" in text else text + "!"
            decision = engine.decision_for(page_index, span.id, new_text)
            dec_str = (
                "None"
                if decision is None
                else (decision.strategy, decision.badge, round(decision.confidence, 2))
            )
            print(
                f"    page{page_index} span{span.id} {text[:34]!r} "
                f"font={span.font_mupdf} size={span.size_mupdf} "
                f"can_edit={reason!r} decision={dec_str}"
            )
            if reason:
                continue
            res = engine.edit_line(page_index, span.id, new_text)
            print(
                f"      edit ok={res.ok} noop={res.noop} badge={res.badge!r} "
                f"overflow={res.overflow} warn={res.warning!r} "
                f"w={res.original_width:.1f}->{res.new_width:.1f}"
            )
            if not res.ok:
                continue
            got = _text(session, page_index)
            print(
                f"      extracted contains typed={new_text in got} "
                f"reversed={new_text[::-1] in got} got={got.strip()[:60]!r}"
            )
            session.history.undo()
            undone = _text(session, page_index)
            print(f"      undo restored original={text in undone}")
    finally:
        session.close()


def main() -> int:
    for case in CASES:
        try:
            _try(case)
        except Exception as exc:
            print(f"=== {case}: EXCEPTION {type(exc).__name__}: {exc}")

    try:
        PdfSession.open(CORPUS / "encrypted.pdf")
    except PasswordRequiredError:
        print("=== encrypted: PasswordRequiredError without password (expected)")
    else:
        raise AssertionError("encrypted.pdf opened without a password")

    session, inspect, engine = _session(CORPUS / "encrypted.pdf", password="secret")
    try:
        model = inspect.model(0)
        span = model.spans[0]
        res = engine.edit_line(0, span.id, span.text + " X")
        print(f"=== encrypted pw=secret: edit ok={res.ok} warn={res.warning!r}")
    finally:
        session.close()
    print("probe_edit_cases: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
