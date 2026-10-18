"""Paragraph layout: word wrapping, alignment and auto-fit for reflow.

This module is geometry/math only: it turns a paragraph's text runs into
concrete lines with baseline positions and optionally shrinks them to fit
a fixed block width.  It never touches the PDF file; the caller (the edit
engine) owns the font slots, the page and the content-stream write.

Coordinate conventions
~~~~~~~~~~~~~~~~~~~~~~
All geometry here is in *page model space* (PyMuPDF convention, origin top
left, y growing downward), matching :class:`PageModel`.  A paragraph is
anchored by a :class:`BlockFrame`: the baseline origin of its first line
plus two unit axes - ``axis`` (the text direction, e.g. ``(1, 0)``) and
``down`` (the direction successive baselines advance, e.g. ``(0, 1)``).

A :class:`FlowBlock` then describes each line purely in local coordinates:
``x`` runs along ``axis`` from the frame origin, ``y`` along ``down``.
Widths stick to the same arithmetic as the edit engine's ``_width_of`` so
a rendered line's advance matches the width we computed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Rect = tuple[float, float, float, float]
Point = tuple[float, float]

_MEASURE_UNKNOWN = 0.55  # em-per-char fallback, mirrors the edit engine


class OverflowMode(StrEnum):
    """What to do when a reflowed paragraph is wider than its block."""

    AUTO_FIT = "auto_fit"
    PUSH_DOWN = "push_down"
    WARN = "warn"

    @classmethod
    def parse(cls, value: str | None) -> OverflowMode:
        try:
            return cls(value or "auto_fit")
        except ValueError:
            return OverflowMode.AUTO_FIT


class Alignment(StrEnum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


@dataclass(frozen=True)
class BlockFrame:
    """Anchor of one paragraph in page model space."""

    origin: Point
    axis: Point  # unit vector, text direction
    down: Point  # unit vector, direction of the next baseline
    alignment: Alignment = Alignment.LEFT

    @classmethod
    def from_direction(cls, origin: Point, direction: Point) -> BlockFrame:
        length = math.hypot(direction[0], direction[1]) or 1.0
        axis = (direction[0] / length, direction[1] / length)
        return cls(origin=origin, axis=axis, down=(-axis[1], axis[0]))


@dataclass(frozen=True)
class RunStyle:
    """Font capability + text parameters for a run of characters."""

    size: float
    char_width: dict[str, int] | None = None  # advance in 1000-em per char
    base14: str | None = None  # measurement fallback when no width table
    tc: float = 0.0
    tw: float = 0.0
    tz: float = 100.0
    ts: float = 0.0
    fill: Any = None  # the run's own fill colour (mixed-style paragraphs)

    def advance(self, text: str) -> float:
        if not text:
            return 0.0
        if self.char_width is not None and all(ch in self.char_width for ch in text):
            base = sum(self.char_width[ch] for ch in text) / 1000.0 * self.size
        elif self.base14:
            try:
                import pymupdf

                font = pymupdf.Font(fontname=self.base14)
                base = font.text_length(text, fontsize=self.size)
            except Exception:  # pragma: no cover - defensive
                base = _MEASURE_UNKNOWN * self.size * len(text)
        else:
            base = _MEASURE_UNKNOWN * self.size * len(text)
        return (base + self.tc * len(text) + self.tw * text.count(" ")) * (
            self.tz / 100.0
        )


@dataclass
class FlowRun:
    style: RunStyle
    text: str
    owner: Any = None  # engine-side slot owning this run's encoding

    @property
    def width(self) -> float:
        return self.style.advance(self.text)


@dataclass
class FlowLine:
    runs: list[FlowRun] = field(default_factory=list)
    y: float = 0.0  # baseline offset along ``down`` (first line is 0)
    x: float = 0.0  # baseline origin offset along ``axis`` after alignment
    width: float = 0.0  # natural advance (before auto-fit scale)
    spaces: int = 0
    justify: bool = False
    space_extra: float = 0.0  # per-gap justification spread (points)

    def chars(self) -> int:
        return sum(len(run.text) for run in self.runs)

    def total(self, tracking: float = 0.0) -> float:
        width = self.width
        if tracking:
            width += tracking * self.chars()
        return width


@dataclass
class FlowBlock:
    lines: list[FlowLine] = field(default_factory=list)
    scale: float = 1.0  # auto-fit factor in [0.9, 1.0], applied via Tz
    tracking: float = 0.0  # extra per-char advance (points, <= 0), via Tc
    width: float = 0.0  # widest natural line width, unscaled
    height: float = 0.0  # last baseline offset in lines (leading applied outside)
    overflow: bool = False
    fit: bool = True
    reason: str = ""

    def widest(self, tracking: float | None = None) -> float:
        return max(
            (line.total(self.tracking if tracking is None else tracking)
             for line in self.lines),
            default=0.0,
        )

    def rendered(self, line: FlowLine) -> float:
        return line.width * self.scale + self.tracking * line.chars()

    def rendered_widest(self) -> float:
        return max((self.rendered(line) for line in self.lines), default=0.0)


_WORD_RE = re.compile(r"\S+\s*")


def split_words(text: str) -> list[str]:
    """Split ``text`` into tokens; a trailing space sticks to its word."""
    return _WORD_RE.findall(text) or ([text] if text.strip() else [])


def _chars_with_styles(runs: list[FlowRun]) -> list[tuple[str, RunStyle, Any]]:
    return [(char, run.style, run.owner) for run in runs for char in run.text]


def _runs_from_chars(chars: list[tuple[str, RunStyle, Any]]) -> list[FlowRun]:
    runs: list[FlowRun] = []
    for char, style, owner in chars:
        if runs and runs[-1].style == style and runs[-1].owner is owner:
            last = runs[-1]
            runs[-1] = FlowRun(style=style, text=last.text + char, owner=owner)
        else:
            runs.append(FlowRun(style=style, text=char, owner=owner))
    return runs


def _last_index(chars: list[tuple[str, RunStyle, Any]]) -> int:
    for index in range(len(chars) - 1, -1, -1):
        if not chars[index][0].isspace():
            return index
    return -1


def wrap_paragraph(
    runs: list[FlowRun],
    available: float,
    *,
    first_indent: float = 0.0,
    hanging: float = 0.0,
) -> tuple[list[FlowLine], bool]:
    """Greedy word wrap into ``available`` local units.

    Returns ``(lines, overflow)``; every line carries its ``x`` offset
    (indent placement) and natural ``width``.  A trailing space is dropped
    at every line break, matching the emitted ``Tj`` payloads.
    """
    chars = _chars_with_styles(runs)
    words: list[list[tuple[str, RunStyle, Any]]] = []
    current: list[tuple[str, RunStyle, Any]] = []
    for item in chars:
        current.append(item)
        if item[0].isspace():
            words.append(current)
            current = []
    if current:
        words.append(current)

    lines: list[FlowLine] = []
    overflow = False

    def build(
        line_chars: list[tuple[str, RunStyle, Any]], indent: float
    ) -> None:
        nonlocal overflow
        trimmed = list(line_chars)
        while trimmed and trimmed[-1][0].isspace():
            trimmed.pop()
        if not trimmed:
            return
        width = sum(style.advance(ch) for ch, style, _owner in trimmed)
        line = FlowLine(
            runs=_runs_from_chars(trimmed),
            width=width,
            spaces=sum(1 for ch, _style, _owner in trimmed if ch.isspace()),
        )
        line.x = indent
        if width > available - indent:
            overflow = True
        lines.append(line)

    indent = first_indent
    current_line: list[tuple[str, RunStyle, Any]] = []
    current_width = 0.0
    for word in words:
        word_width = sum(style.advance(ch) for ch, style, _owner in word)
        if current_line and current_width + word_width > available - indent:
            build(current_line, indent)
            current_line = []
            current_width = 0.0
            indent = hanging
        current_line.extend(word)
        current_width += word_width
    if current_line:
        build(current_line, indent)

    for index, line in enumerate(lines):
        line.y = float(index)
    return lines, overflow


def align_lines(
    lines: list[FlowLine],
    available: float,
    alignment: Alignment | str | None,
) -> None:
    mode = (
        Alignment(alignment)
        if isinstance(alignment, str)
        else (alignment or Alignment.LEFT)
    )
    for index, line in enumerate(lines):
        is_last = index == len(lines) - 1
        gap = available - line.width
        if mode is Alignment.RIGHT:
            line.x += max(0.0, gap)
        elif mode is Alignment.CENTER:
            line.x += max(0.0, gap) / 2.0
        elif mode is Alignment.JUSTIFY and not is_last:
            ended_in_space = bool(
                line.runs and line.runs[-1].text and line.runs[-1].text[-1].isspace()
            )
            if line.spaces > 0 and gap > 0.5 and not ended_in_space:
                line.justify = True
                line.space_extra = gap / line.spaces
                line.width = available
            else:
                line.x += max(0.0, gap)


def apply_auto_fit(
    flow: FlowBlock,
    available: float,
    *,
    max_shrink: float = 0.90,
    max_tracking: float = 0.02,
    size: float = 10.0,
) -> FlowBlock:
    """Shrink ``flow`` so its widest line fits ``available``.

    Scale (via horizontal scaling) never goes below ``max_shrink``; up to
    ``max_tracking * size`` points of negative character tracking may be
    added at that floor.  ``flow.overflow`` is set when the combined floor
    still cannot fit.
    """
    if not flow.lines:
        return flow
    if flow.widest() <= available:
        return flow
    scale = available / flow.widest() if flow.widest() else 1.0
    if scale >= max_shrink:
        flow.scale = scale
        return flow
    flow.scale = max_shrink
    chars = max(line.chars() for line in flow.lines)
    if chars:
        tracking = (available - flow.widest() * flow.scale) / chars
        if tracking >= -max_tracking * size:
            flow.tracking = tracking
    flow.overflow = flow.rendered_widest() > available
    flow.fit = not flow.overflow
    if flow.overflow:
        flow.reason = (
            "auto-fit floor reached (10% shrink + 2% tracking) "
            "and the paragraph still overflows"
        )
    return flow


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def block_alignment(
    x0s: list[float],
    x1s: list[float],
    block_x0: float,
    block_x1: float,
) -> Alignment:
    """Classify a paragraph's alignment inside a column.

    ``x0s``/``x1s`` are line start/end offsets relative to ``block_x0``;
    ``block_x1`` is the column's right edge, so ``available = x1 - x0``.
    """
    n = len(x0s)
    if n == 0:
        return Alignment.LEFT
    first = x0s[0]
    trailing = x0s[1:]
    if trailing and first - _median(trailing) > 3.0:
        return Alignment.LEFT  # first-line indent (hanging) paragraph
    if trailing:
        flush_left = max(abs(value - block_x0) for value in trailing) <= 3.0
    else:
        flush_left = first - block_x0 <= 3.0
    flush_right = max(abs(block_x1 - value) for value in x1s) <= 3.0
    full_lines = sum(1 for value in x1s if block_x1 - value <= 3.0)
    if n >= 2 and flush_left and full_lines >= n - 1:
        return Alignment.JUSTIFY
    if flush_right and not flush_left:
        return Alignment.RIGHT
    if not flush_left:
        center = block_x0 + (block_x1 - block_x0) / 2.0
        span_center = (min(x0s) + max(x1s)) / 2.0
        if abs(span_center - center) <= 2.0:
            return Alignment.CENTER
    return Alignment.LEFT


def intersect(a: Rect, b: Rect, epsilon: float = 0.5) -> bool:
    return not (
        a[2] <= b[0] + epsilon
        or a[0] >= b[2] - epsilon
        or a[3] <= b[1] + epsilon
        or a[1] >= b[3] - epsilon
    )


def rect_union(rects: list[Rect]) -> Rect:
    if not rects:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    )


_BULLET_RE = re.compile(r"^[\u2022\u2023\u25E6\u2043\u00B7-]")
_NUMBER_RE = re.compile(r"^\s*(\d{1,3}[.).]|[a-zA-Z][.)])\s+")


def looks_like_bullet(text: str) -> bool:
    return bool(_BULLET_RE.match(text) or _NUMBER_RE.match(text))


def flow_bbox_model(
    frame: BlockFrame,
    flow: FlowBlock,
    leading: float,
    *,
    size: float = 10.0,
) -> Rect:
    """Bounding box of a flow in model points (glyph boxes, y down)."""
    if not flow.lines:
        return (frame.origin[0], frame.origin[1], frame.origin[0], frame.origin[1])
    ax, ay = frame.axis
    dx, dy = frame.down
    ox, oy = frame.origin
    xs: list[float] = []
    ys: list[float] = []
    half = max(size * 0.5, 1.0)
    for line in flow.lines:
        depth = line.y * leading
        u0 = line.x
        u1 = line.x + line.total(flow.tracking) * flow.scale
        p0 = (ox + ax * u0 + dx * depth, oy + ay * u0 + dy * depth)
        p1 = (ox + ax * u1 + dx * depth, oy + ay * u1 + dy * depth)
        xs.append(min(p0[0], p1[0]) - half * abs(ax))
        xs.append(max(p0[0], p1[0]) + half * abs(ax))
        ys.append(min(p0[1], p1[1]) - half)
        ys.append(max(p0[1], p1[1]) + half)
    return (min(xs), min(ys), max(xs), max(ys))
