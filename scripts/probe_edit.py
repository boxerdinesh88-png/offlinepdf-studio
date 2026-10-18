import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf

from offlinepdf.core.document import PdfSession
from offlinepdf.core.edit_engine import EditEngine
from offlinepdf.core.font_resolver import FontResolver
from offlinepdf.core.inspect_session import InspectSession


def run(path: str) -> None:
    session = PdfSession.open(path)
    inspect = InspectSession(session)
    inspect.invalidate()
    engine = EditEngine(session, inspect, FontResolver())
    model = inspect.model(0)
    span = model.spans[0]
    old = span.text
    print(f"page0 span{span.id}: {old!r}")

    before_bytes = session.to_bytes()
    res = engine.edit_line(0, span.id, old)
    same = session.to_bytes() == before_bytes
    print(
        f"same-text: ok={res.ok} noop={res.noop} bytes_unchanged={same} "
        f"badge={res.badge!r} path={res.path} warn={res.warning!r} "
        f"ms={res.duration_ms:.1f}"
    )

    new_text = (old + " EDITED").strip() if old else "EDITED"
    res2 = engine.edit_line(0, span.id, new_text)
    print(
        f"changed: ok={res2.ok} noop={res2.noop} badge={res2.badge!r} "
        f"path={res2.path} overflow={res2.overflow} "
        f"widths={res2.original_width:.1f}->{res2.new_width:.1f} "
        f"warn={res2.warning!r} ms={res2.duration_ms:.1f}"
    )
    after_bytes = session.to_bytes()
    doc = pymupdf.open(stream=after_bytes, filetype="pdf")
    text = doc[0].get_text()
    doc.close()
    print(f"extraction contains new text: {new_text in text}")
    print(f"dirty={session.is_dirty}")

    session.history.undo()
    doc = pymupdf.open(stream=session.to_bytes(), filetype="pdf")
    text = doc[0].get_text()
    doc.close()
    print(f"undo: original text back: {old in text}")
    session.history.redo()
    doc = pymupdf.open(stream=session.to_bytes(), filetype="pdf")
    text = doc[0].get_text()
    doc.close()
    print(f"redo: new text back: {new_text in text}")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "tests/_corpus/basic.pdf")
