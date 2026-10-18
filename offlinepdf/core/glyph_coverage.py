"""Glyph coverage: which characters their assigned font cannot render.

A char counts as *missing* only when we positively know the font's unicode
coverage (embedded font with a cmap, CFF charset, or a base-14 family with a
WinAnsi estimate) and the char's codepoint is absent.  Fonts whose coverage
cannot be determined contribute to ``unknown_chars`` instead, so reports never
claim a false missing glyph.

One FontPrograms cache is shared across the whole document session; font
objects are keyed by objgen, which is document-global.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from offlinepdf.core.font_program import FontPrograms, strip_subset_prefix
from offlinepdf.core.text_model import PageModel


@dataclass(frozen=True)
class MissingGlyph:
    """One (character, font) pair that cannot be rendered."""

    char: str
    codepoint: str
    count: int
    font: str
    font_source: str
    page_indices: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "char": self.char,
            "codepoint": self.codepoint,
            "count": self.count,
            "font": self.font,
            "font_source": self.font_source,
            "page_indices": list(self.page_indices),
        }


@dataclass
class PageCoverage:
    """Coverage summary for a single page."""

    page_index: int
    total_chars: int
    checked_chars: int
    missing_chars: int
    unknown_chars: int
    fonts: tuple[str, ...]
    missing: tuple[MissingGlyph, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "page_index": self.page_index,
            "total_chars": self.total_chars,
            "checked_chars": self.checked_chars,
            "missing_chars": self.missing_chars,
            "unknown_chars": self.unknown_chars,
            "fonts": list(self.fonts),
            "missing": [m.to_dict() for m in self.missing],
        }


@dataclass
class DocumentCoverage:
    """Coverage aggregated across pages."""

    pages: tuple[PageCoverage, ...]
    total_chars: int
    checked_chars: int
    missing_chars: int
    unknown_chars: int
    missing: tuple[MissingGlyph, ...]

    @property
    def ok(self) -> bool:
        return self.missing_chars == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "total_chars": self.total_chars,
            "checked_chars": self.checked_chars,
            "missing_chars": self.missing_chars,
            "unknown_chars": self.unknown_chars,
            "missing": [m.to_dict() for m in self.missing],
            "pages": [p.to_dict() for p in self.pages],
        }


def _codepoint_label(char: str) -> str:
    return "-".join(f"U+{ord(c):04X}" for c in char)


def page_coverage(
    model: PageModel, programs: FontPrograms
) -> PageCoverage:
    """Compute coverage for one page (uses the shared document cache)."""
    total = 0
    checked = 0
    missing_count = 0
    unknown = 0
    font_names: set[str] = set()
    counter: Counter[tuple[str, str, str, str]] = Counter()

    for text_char, _span in model.chars:
        ch = text_char.ch
        n = len(ch)
        if n == 0:
            continue
        total += n
        event = None
        if 0 <= text_char.event_idx < len(model.shows):
            event = model.shows[text_char.event_idx]
        ref = event.font if event is not None else None
        if ref is None:
            # extraction fallback: no reliable font object -> unknown
            unknown += n
            continue
        program = programs.get(ref)
        font_names.add(strip_subset_prefix(ref.base_font))
        if not program.coverage_known:
            unknown += n
            continue
        checked += n
        if not program.covers(ch):
            missing_count += n
            counter[(ch, program.base_font, program.source, ref.base_font)] += n

    missing = [
        MissingGlyph(
            char=key[0],
            codepoint=_codepoint_label(key[0]),
            count=count,
            font=key[1],
            font_source=key[2],
            page_indices=(model.page_index,),
        )
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0][0]))
    ]
    return PageCoverage(
        page_index=model.page_index,
        total_chars=total,
        checked_chars=checked,
        missing_chars=missing_count,
        unknown_chars=unknown,
        fonts=tuple(sorted(font_names)),
        missing=tuple(missing),
    )


@dataclass
class _AggRow:
    char: str
    font: str
    font_source: str
    count: int = 0
    pages: set[int] = field(default_factory=set)


def aggregate_coverage(pages: list[PageCoverage]) -> DocumentCoverage:
    """Merge per-page coverage rows into a document-level summary."""
    merged: dict[tuple[str, str], _AggRow] = {}
    total = checked = missing = unknown = 0
    for page in pages:
        total += page.total_chars
        checked += page.checked_chars
        missing += page.missing_chars
        unknown += page.unknown_chars
        for item in page.missing:
            row_key = (item.char, item.font)
            row = merged.get(row_key)
            if row is None:
                row = _AggRow(item.char, item.font, item.font_source)
                merged[row_key] = row
            row.count += item.count
            row.pages.update(item.page_indices)
    aggregated = sorted(
        merged.values(),
        key=lambda row: (-row.count, row.char, row.font),
    )
    return DocumentCoverage(
        pages=tuple(pages),
        total_chars=total,
        checked_chars=checked,
        missing_chars=missing,
        unknown_chars=unknown,
        missing=tuple(
            MissingGlyph(
                char=row.char,
                codepoint=_codepoint_label(row.char),
                count=row.count,
                font=row.font,
                font_source=row.font_source,
                page_indices=tuple(sorted(row.pages)),
            )
            for row in aggregated
        ),
    )
