"""Text model: page -> block -> line -> span -> char with per-char geometry.

Milestone M2 structure::

    PageModel
      blocks: list[TextBlock]        # XY-cut reading order groups
        lines: list[TextLine]        # one baseline run
          spans: list[TextSpan]      # contiguous run with identical style
            chars: list[TextChar]    # glyph box + raw PDF style values

Geometry is kept in *PyMuPDF space* (origin top-left, y down, unrotated
regardless of ``/Rotate``); the content stream parser works in PDF user space
(y up). Conversion between the two uses the page's effective top edge:
``y_mupdf = box.y1 - y_pdf``.

Every char remembers which :class:`~offlinepdf.core.content_stream.ShowEvent` produced
it (``event_idx``); :meth:`PageModel.char_style` resolves that into a
:class:`CharStyle` with the raw stream values, falling back to MuPDF
extraction data when no stream event could be matched.

Reading order uses a recursive XY-cut over *lines* (not MuPDF's blocks, which
can merge across columns): full-width row gaps first, then column gutters.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

import pikepdf
import pymupdf

from offlinepdf.core.content_stream import (
    ColorValue,
    FontRef,
    Matrix,
    PaintRecord,
    ShowEvent,
    mat_apply,
    parse_page_content,
)

Rect = tuple[float, float, float, float]
Point = tuple[float, float]

_ROW_GAP = 2.0
_COL_GAP = 6.0
_ANCHOR_TOL = 0.75  # pt; event origin vs char origin
_HIT_TOL = 1.5  # pt; near-miss tolerance for clicks


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #
@dataclass
class TextChar:
    """One glyph box (PyMuPDF space) plus its matched stream event."""

    ch: str
    bbox: Rect
    origin: Point
    event_idx: int = -1
    span: TextSpan | None = None


@dataclass
class TextSpan:
    """Contiguous run of chars sharing extraction style."""

    chars: list[TextChar]
    bbox: Rect
    font_mupdf: str
    size_mupdf: float
    flags: int
    color_int: int
    ascender: float
    descender: float
    id: int = -1
    line: TextLine | None = None

    @property
    def text(self) -> str:
        return "".join(c.ch for c in self.chars)

    @property
    def origin(self) -> Point:
        return self.chars[0].origin if self.chars else (self.bbox[0], self.bbox[1])


@dataclass
class TextLine:
    """One baseline run (``dir`` is the MuPDF line direction, e.g. (1, 0))."""

    spans: list[TextSpan]
    bbox: Rect
    dir: Point
    block: TextBlock | None = None

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def origin(self) -> Point:
        for span in self.spans:
            if span.chars:
                return span.chars[0].origin
        return (self.bbox[0], self.bbox[1])


@dataclass
class TextBlock:
    """Group of lines produced by the XY-cut (usually a paragraph)."""

    lines: list[TextLine]
    bbox: Rect

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True)
class CharStyle:
    """Raw style values behind one char (stream values preferred)."""

    source: str  # "stream" | "extraction"
    event_seq: int  # -1 for extraction fallback
    font: FontRef | None
    tf_size: float
    size: float
    tc: float
    tw: float
    tz: float
    ts: float
    tr: int
    fill: ColorValue | None
    stroke: ColorValue | None
    fill_alpha: float
    stroke_alpha: float
    color_int: int  # MuPDF span colour (0xRRGGBB), -1 if unknown
    span_font_name: str
    span_size: float
    span_flags: int


@dataclass
class HitResult:
    """Result of clicking/hovering a position that landed on a char."""

    page_index: int
    char: TextChar
    span: TextSpan
    line: TextLine
    block: TextBlock


# --------------------------------------------------------------------------- #
# page model
# --------------------------------------------------------------------------- #
@dataclass
class PageModel:
    """Everything known about one page's text layer + painted paths."""

    page_index: int
    rotation: int  # page /Rotate (0/90/180/270)
    mediabox: Rect  # effective box in PDF user space (y up)
    size_unrot: Point  # (w, h) in model space (y down, unrotated)
    size_view: Point  # displayed (w, h) after /Rotate
    rotation_matrix: Matrix  # model space -> view space (both y down)
    blocks: list[TextBlock] = field(default_factory=list)
    spans: list[TextSpan] = field(default_factory=list)
    chars: list[tuple[TextChar, TextSpan]] = field(default_factory=list)
    shows: list[ShowEvent] = field(default_factory=list)
    paints: list[PaintRecord] = field(default_factory=list)
    fonts: dict[str, FontRef] = field(default_factory=dict)
    font_dicts: dict[str, pikepdf.Dictionary] = field(default_factory=dict)
    has_text_layer: bool = False
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    @classmethod
    def build(
        cls,
        pdf: pikepdf.Pdf,
        fitz_doc: pymupdf.Document,
        page_index: int,
    ) -> PageModel:
        """Extract + parse ``page_index`` and assemble the model."""
        fitz_page = fitz_doc[page_index]
        raw = fitz_page.get_text("rawdict", sort=False)
        pdf_page = pdf.pages[page_index]
        parsed = parse_page_content(pdf, pdf_page)
        top = _page_top(pdf_page)
        rotation = int(fitz_page.rotation)

        lines: list[TextLine] = []
        flat: list[tuple[TextChar, TextSpan]] = []
        for block in raw.get("blocks", ()):
            if block.get("type", 0) != 0:
                continue  # image block
            for raw_line in block.get("lines", ()):
                spans: list[TextSpan] = []
                for raw_span in raw_line.get("spans", ()):
                    chars = [
                        TextChar(
                            ch=str(c.get("c", "")),
                            bbox=_rect(c.get("bbox")),
                            origin=_point(c.get("origin")),
                        )
                        for c in raw_span.get("chars", ())
                    ]
                    if not chars:
                        continue
                    span = TextSpan(
                        chars=chars,
                        bbox=_rect(raw_span.get("bbox")),
                        font_mupdf=str(raw_span.get("font", "")),
                        size_mupdf=float(raw_span.get("size", 0.0)),
                        flags=int(raw_span.get("flags", 0)),
                        color_int=int(raw_span.get("color", -1)),
                        ascender=float(raw_span.get("ascender", 0.0)),
                        descender=float(raw_span.get("descender", 0.0)),
                    )
                    for char in chars:
                        char.span = span
                    spans.append(span)
                    flat.extend((char, span) for char in chars)
                if not spans:
                    continue
                line = TextLine(
                    spans=spans,
                    bbox=_rect(raw_line.get("bbox")),
                    dir=_point(raw_line.get("dir")) or (1.0, 0.0),
                )
                for span in spans:
                    span.line = line
                lines.append(line)

        assignment = _assign_events(flat, parsed.shows, top)
        for (char, _span), event_idx in zip(flat, assignment, strict=True):
            char.event_idx = event_idx

        blocks = _xy_cut(lines)
        spans_flat: list[TextSpan] = []
        for block in blocks:
            for line in block.lines:
                for span in line.spans:
                    span.id = len(spans_flat)
                    spans_flat.append(span)

        rotation_matrix = _rotation_matrix(fitz_page)
        width = float(fitz_page.rect.width)
        height = float(fitz_page.rect.height)
        size_unrot = (height, width) if rotation in (90, 270) else (width, height)
        size_view = (width, height)

        return cls(
            page_index=page_index,
            rotation=rotation,
            mediabox=_effective_box(pdf_page),
            size_unrot=size_unrot,
            size_view=size_view,
            rotation_matrix=rotation_matrix,
            blocks=blocks,
            spans=spans_flat,
            chars=flat,
            shows=parsed.shows,
            paints=parsed.paints,
            fonts=parsed.fonts,
            font_dicts=parsed.font_dicts,
            has_text_layer=bool(spans_flat),
            warnings=list(parsed.warnings),
        )

    # ------------------------------------------------------------------ #
    # coordinate space helpers
    # ------------------------------------------------------------------ #
    def to_view(self, x: float, y: float) -> Point:
        """Model space (y down, unrotated) -> displayed view space."""
        if self.rotation == 0:
            return (x, y)
        return mat_apply(self.rotation_matrix, x, y)

    def from_view(self, x: float, y: float) -> Point:
        """Displayed view space -> model space."""
        if self.rotation == 0:
            return (x, y)
        m = self.rotation_matrix
        det = m[0] * m[3] - m[1] * m[2]
        if abs(det) < 1e-12:
            return (x, y)
        a, b, c, d, e, f = m
        inverse: Matrix = (
            d / det,
            -b / det,
            -c / det,
            a / det,
            (c * f - d * e) / det,
            (b * e - a * f) / det,
        )
        return mat_apply(inverse, x, y)

    def view_rect(self, rect: Rect) -> Rect:
        """Transform a model-space rect into a displayed (view) rect."""
        x0, y0, x1, y1 = rect
        corners = [self.to_view(x, y) for x in (x0, x1) for y in (y0, y1)]
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        return (min(xs), min(ys), max(xs), max(ys))

    # ------------------------------------------------------------------ #
    # lookups
    # ------------------------------------------------------------------ #
    @property
    def content_rect(self) -> Rect:
        """Union of all block bboxes (empty rect when there is no text)."""
        if not self.blocks:
            return (0.0, 0.0, 0.0, 0.0)
        return _union(block.bbox for block in self.blocks)

    def char_style(self, char: TextChar) -> CharStyle:
        """Raw style values for ``char`` (stream event preferred)."""
        span = char.span
        font_name = span.font_mupdf if span else ""
        size = span.size_mupdf if span else 0.0
        flags = span.flags if span else 0
        color_int = span.color_int if span else -1
        if char.event_idx >= 0:
            event = self.shows[char.event_idx]
            return CharStyle(
                source="stream",
                event_seq=event.seq,
                font=event.font,
                tf_size=event.tf_size,
                size=event.size,
                tc=event.tc,
                tw=event.tw,
                tz=event.tz,
                ts=event.ts,
                tr=event.tr,
                fill=event.fill,
                stroke=event.stroke,
                fill_alpha=event.fill_alpha,
                stroke_alpha=event.stroke_alpha,
                color_int=color_int,
                span_font_name=font_name,
                span_size=size,
                span_flags=flags,
            )
        return CharStyle(
            source="extraction",
            event_seq=-1,
            font=None,
            tf_size=size,
            size=size,
            tc=0.0,
            tw=0.0,
            tz=100.0,
            ts=0.0,
            tr=0,
            fill=_fill_from_int(color_int),
            stroke=None,
            fill_alpha=1.0,
            stroke_alpha=1.0,
            color_int=color_int,
            span_font_name=font_name,
            span_size=size,
            span_flags=flags,
        )

    def hit_test(self, x: float, y: float) -> HitResult | None:
        """Find the char at model-space ``(x, y)`` (near misses allowed)."""
        if not self.has_text_layer:
            return None
        best: TextChar | None = None
        best_area = math.inf
        for char, _span in self.chars:
            x0, y0, x1, y1 = char.bbox
            if x0 <= x <= x1 and y0 <= y <= y1:
                area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
                if area < best_area:
                    best, best_area = char, area
        if best is None:
            distance = _HIT_TOL
            for char, _span in self.chars:
                ox, oy = char.origin
                delta = math.hypot(ox - x, oy - y)
                if delta <= distance:
                    best, distance = char, delta
        if best is None:
            return None
        span = best.span
        line = span.line if span else None
        block = line.block if line else None
        if span is None or line is None or block is None:
            return None
        return HitResult(
            page_index=self.page_index,
            char=best,
            span=span,
            line=line,
            block=block,
        )

    def hit_test_view(self, x: float, y: float) -> HitResult | None:
        """Hit test from displayed (view) coordinates."""
        mx, my = self.from_view(x, y)
        return self.hit_test(mx, my)

    # ------------------------------------------------------------------ #
    # debug geometry (model space; the session converts to view space)
    # ------------------------------------------------------------------ #
    def debug_geometry(self) -> dict[str, list[object]]:
        """Rectangles + baselines for the overlay, in model coordinates."""
        baselines: list[object] = []
        for block in self.blocks:
            for line in block.lines:
                points: list[Point] = []
                for span in line.spans:
                    if span.chars:
                        points.append(span.chars[0].origin)
                        points.append(span.chars[-1].origin)
                if len(points) >= 2:
                    baselines.append((points[0], points[-1]))
        return {
            "blocks": [block.bbox for block in self.blocks],
            "spans": [span.bbox for span in self.spans],
            "chars": [char.bbox for char, _ in self.chars],
            "baselines": baselines,
        }


# --------------------------------------------------------------------------- #
# event <-> char assignment
# --------------------------------------------------------------------------- #
def _assign_events(
    flat: list[tuple[TextChar, TextSpan]],
    shows: list[ShowEvent],
    top: float,
) -> list[int]:
    """Match stream events to extracted chars; returns one index per char."""
    count = len(flat)
    if count == 0:
        return []
    if not shows:
        return [-1] * count

    assignment = [-1] * count
    anchors = _find_anchors(flat, shows, top)

    # drop anchors inconsistent with draw order
    filtered: list[tuple[int, int]] = []
    last_event, last_char = -1, -1
    for event_idx, char_idx in anchors:
        if event_idx > last_event and char_idx > last_char:
            filtered.append((event_idx, char_idx))
            last_event, last_char = event_idx, char_idx

    seq: list[tuple[int, int]] = [*filtered, (len(shows), count)]
    previous_event = -1
    cursor = 0
    for position, (event_idx, char_idx) in enumerate(seq):
        next_char = seq[position + 1][1] if position + 1 < len(seq) else count
        # events strictly between the previous anchor and this one
        segment = list(range(previous_event + 1, event_idx))
        limit = char_idx
        pos = cursor
        for idx in segment:
            take = max(0, shows[idx].glyph_count)
            end = min(pos + take, limit)
            for char_pos in range(pos, end):
                assignment[char_pos] = idx
            pos = end
            if pos >= limit:
                break
        if pos < limit:
            # stream undercounted: give leftovers to the closest previous event
            owner = segment[-1] if segment else previous_event
            if owner >= 0:
                for char_pos in range(pos, limit):
                    assignment[char_pos] = owner
        if event_idx < len(shows):
            take = max(1, shows[event_idx].glyph_count)
            end = min(char_idx + take, next_char)
            for char_pos in range(char_idx, end):
                assignment[char_pos] = event_idx
            cursor = max(end, char_idx)
        else:
            cursor = max(limit, cursor)
        previous_event = event_idx
    return assignment


def _find_anchors(
    flat: list[tuple[TextChar, TextSpan]],
    shows: list[ShowEvent],
    top: float,
) -> list[tuple[int, int]]:
    """Pair events with known origins to the nearest extracted char."""
    cell = 4.0
    grid: dict[tuple[int, int], list[int]] = {}
    for index, (char, _span) in enumerate(flat):
        gx = math.floor(char.origin[0] / cell)
        gy = math.floor(char.origin[1] / cell)
        grid.setdefault((gx, gy), []).append(index)

    claimed: set[int] = set()
    anchors: list[tuple[int, int]] = []
    for event_idx, event in enumerate(shows):
        if not event.origin_known:
            continue
        ox = event.origin[0]
        oy = top - event.origin[1]
        gx0 = math.floor((ox - _ANCHOR_TOL) / cell)
        gx1 = math.floor((ox + _ANCHOR_TOL) / cell)
        gy0 = math.floor((oy - _ANCHOR_TOL) / cell)
        gy1 = math.floor((oy + _ANCHOR_TOL) / cell)
        best_idx = -1
        best_dist = _ANCHOR_TOL
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                for char_idx in grid.get((gx, gy), ()):
                    if char_idx in claimed:
                        continue
                    char = flat[char_idx][0]
                    distance = math.hypot(char.origin[0] - ox, char.origin[1] - oy)
                    if distance <= best_dist:
                        best_dist = distance
                        best_idx = char_idx
        if best_idx >= 0:
            claimed.add(best_idx)
            anchors.append((event_idx, best_idx))
    return anchors


# --------------------------------------------------------------------------- #
# reading order (XY-cut over lines)
# --------------------------------------------------------------------------- #
def _xy_cut(lines: list[TextLine]) -> list[TextBlock]:
    if not lines:
        return []
    boxes: list[tuple[TextLine, Rect]] = [(line, line.bbox) for line in lines]
    groups = _split(boxes)
    blocks: list[TextBlock] = []
    for group in groups:
        if not group:
            continue
        group.sort(key=lambda item: (item[1][1], item[1][0]))
        block = TextBlock(
            lines=[line for line, _ in group],
            bbox=_union(rect for _, rect in group),
        )
        for line in block.lines:
            line.block = block
        blocks.append(block)
    return blocks


def _split(boxes: list[tuple[TextLine, Rect]]) -> list[list[tuple[TextLine, Rect]]]:
    if len(boxes) <= 1:
        return [boxes] if boxes else []
    rows = _row_split(boxes)
    if len(rows) > 1:
        return [group for row in rows for group in _split(row)]
    columns = _col_split(boxes)
    if len(columns) > 1:
        return [group for column in columns for group in _split(column)]
    return [sorted(boxes, key=lambda item: (item[1][1], item[1][0]))]


def _row_split(
    boxes: list[tuple[TextLine, Rect]],
) -> list[list[tuple[TextLine, Rect]]]:
    ordered = sorted(boxes, key=lambda item: (item[1][1], item[1][0]))
    # Adaptive threshold: paragraph gaps are noticeably larger than the
    # typical gap between lines of one paragraph. Overlapping lines (multi-
    # column) are excluded from the median so columns never split into rows.
    gaps = [
        max(0.0, ordered[i + 1][1][1] - ordered[i][1][3])
        for i in range(len(ordered) - 1)
    ]
    positives = sorted(g for g in gaps if g > 0.5)
    threshold = _ROW_GAP
    if positives:
        small = [g for g in positives if g <= 4 * _ROW_GAP]
        if small:
            median = small[len(small) // 2]
            threshold = max(_ROW_GAP, 1.5 * median)

    rows: list[list[tuple[TextLine, Rect]]] = []
    if not ordered:
        return rows
    current: list[tuple[TextLine, Rect]] = [ordered[0]]
    max_y1 = ordered[0][1][3]
    for position in range(1, len(ordered)):
        item = ordered[position]
        gap = gaps[position - 1]
        if gap > threshold and item[1][1] >= max_y1 + _ROW_GAP:
            rows.append(current)
            current = []
            max_y1 = -math.inf
        current.append(item)
        max_y1 = max(max_y1, item[1][3])
    rows.append(current)
    return rows


def _col_split(
    boxes: list[tuple[TextLine, Rect]],
) -> list[list[tuple[TextLine, Rect]]]:
    ordered = sorted(boxes, key=lambda item: (item[1][0], item[1][1]))
    columns: list[list[tuple[TextLine, Rect]]] = []
    current: list[tuple[TextLine, Rect]] = []
    max_x1 = -math.inf
    for item in ordered:
        left_edge = item[1][0]
        if current and left_edge >= max_x1 + _COL_GAP:
            columns.append(current)
            current = []
            max_x1 = -math.inf
        current.append(item)
        max_x1 = max(max_x1, item[1][2])
    if current:
        columns.append(current)
    return columns


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
def _rect(value: object) -> Rect:
    try:
        seq = list(value)  # type: ignore[call-overload]
        return (float(seq[0]), float(seq[1]), float(seq[2]), float(seq[3]))
    except Exception:  # pragma: no cover - malformed bbox
        return (0.0, 0.0, 0.0, 0.0)


def _point(value: object) -> Point:
    try:
        seq = list(value)  # type: ignore[call-overload]
        return (float(seq[0]), float(seq[1]))
    except Exception:  # pragma: no cover - malformed origin
        return (0.0, 0.0)


def _union(rects: Iterable[Rect]) -> Rect:
    items = list(rects)
    if not items:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(r[0] for r in items),
        min(r[1] for r in items),
        max(r[2] for r in items),
        max(r[3] for r in items),
    )


def _effective_box(page: pikepdf.Page) -> Rect:
    """Intersection of CropBox and MediaBox as ``(x0, y0, x1, y1)`` y up."""
    boxes: list[Rect] = []
    for key in ("/MediaBox", "/CropBox"):
        raw = page.get(key)
        if isinstance(raw, pikepdf.Array) and len(raw) == 4:
            try:
                boxes.append(tuple(float(v) for v in raw))  # type: ignore[arg-type]
            except Exception:  # pragma: no cover - malformed box
                continue
    if not boxes:
        return (0.0, 0.0, 612.0, 792.0)
    return (
        max(b[0] for b in boxes),
        max(b[1] for b in boxes),
        min(b[2] for b in boxes),
        min(b[3] for b in boxes),
    )


def _page_top(page: pikepdf.Page) -> float:
    """Top edge of the page in PDF user space (used for the y flip)."""
    return _effective_box(page)[3]


def _rotation_matrix(fitz_page: pymupdf.Page) -> Matrix:
    """MuPDF's matrix: unrotated model space -> displayed view space.

    Taken straight from PyMuPDF so translations for /Rotate 90/180/270
    always match what the renderer produces.
    """
    matrix = fitz_page.rotation_matrix
    return (
        float(matrix.a),
        float(matrix.b),
        float(matrix.c),
        float(matrix.d),
        float(matrix.e),
        float(matrix.f),
    )


def _fill_from_int(color_int: int) -> ColorValue | None:
    if color_int < 0:
        return None
    red = (color_int >> 16) & 0xFF
    green = (color_int >> 8) & 0xFF
    blue = color_int & 0xFF
    return ColorValue("DeviceRGB", (red / 255.0, green / 255.0, blue / 255.0))
