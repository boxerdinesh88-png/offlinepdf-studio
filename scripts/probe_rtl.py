"""Probe: why the RTL line refuses editing (char->event assignment)."""

from __future__ import annotations

from pathlib import Path

from offlinepdf.core.document import PdfSession
from offlinepdf.core.edit_engine import EditEngine
from offlinepdf.core.font_resolver import FontResolver
from offlinepdf.core.inspect_session import InspectSession

CORPUS = Path("tests/_corpus")


def main() -> int:
    session = PdfSession.open(CORPUS / "rtl.pdf")
    inspect = InspectSession(session)
    inspect.invalidate()
    engine = EditEngine(session, inspect, FontResolver())
    model = inspect.model(0)
    print(f"events={len(model.shows)} spans={len(model.spans)}")
    for i, event in enumerate(model.shows):
        print(
            f"  show[{i}] seq={event.seq} glyphs={event.glyph_count} "
            f"origin={event.origin} known={event.origin_known} "
            f"text={getattr(event, 'text', None)!r}"
        )
    for span in model.spans:
        print(f"  span{span.id} {span.text!r} font={span.font_mupdf}")
        for char in span.chars:
            print(
                f"    {char.ch!r} event_idx={char.event_idx} "
                f"origin=({char.origin[0]:.1f},{char.origin[1]:.1f})"
            )
        print(f"    can_edit={engine.can_edit(0, span.id)!r}")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
