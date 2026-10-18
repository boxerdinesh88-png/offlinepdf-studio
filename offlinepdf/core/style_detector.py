"""Style detection pipeline: from page model to a StyleProfile.

Pipeline (docs/SPEC.md section 7):
  1. extract content stream events            (offlinepdf.core.content_stream)
  2. build page model, assign chars to events (offlinepdf.core.text_model)
  3. per-span measurements (size/family/weight/italic/color/alignment)
  4. pixel self-check for non-standard colour spaces (<= 3 adjustments)
  5. decorations from painted paths + link annotations
  6. cluster spans into style groups; derive the six presets
  7. page-level fields (dominant style, alignment, line height)

Every reported field carries a confidence (0..1) and an origin:
"detected" | "inherited" | "user_override".  Presets are always emitted
(six of them) with a ``found`` flag; unfound presets use documented
defaults with origin "inherited".
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise
from statistics import median
from typing import Any

from offlinepdf.core.content_stream import ColorValue
from offlinepdf.core.font_program import (
    FontPrograms,
    family_from_base,
    weight_from_base,
)
from offlinepdf.core.text_model import PageModel, TextBlock, TextSpan

ORIGIN_DETECTED = "detected"
ORIGIN_INHERITED = "inherited"
ORIGIN_OVERRIDE = "user_override"

ALIGN_LEFT = "left"
ALIGN_RIGHT = "right"
ALIGN_CENTER = "center"
ALIGN_JUSTIFY = "justify"

# pixel self-check: DeltaE (CIE76, sRGB) below this counts as verified
_COLOR_TOLERANCE = 8.0
_COLOR_MAX_PASSES = 3
_ALIGN_TOL = 1.5  # pt

PRESET_NAMES = ("Body", "H1", "H2", "H3", "Caption", "Footer")

PixelSampler = Callable[[float, float, float, float], list[tuple[int, int, int]]]


# --------------------------------------------------------------------------- #
# colour helpers
# --------------------------------------------------------------------------- #
def _naive_cmyk_to_rgb(components: tuple[float, ...]) -> tuple[float, float, float]:
    c, m, y, k = [*components, 0.0, 0.0, 0.0, 0.0][:4]
    return ((1.0 - c) * (1.0 - k), (1.0 - m) * (1.0 - k), (1.0 - y) * (1.0 - k))


def _to_linear(channel: float) -> float:
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _rgb_to_lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = (_to_linear(c) for c in rgb)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    x /= 0.95047
    z /= 1.08883

    def f(value: float) -> float:
        if value > 0.008856:
            return value ** (1.0 / 3.0)
        return 7.787 * value + 16.0 / 116.0

    fx, fy, fz = f(x), f(y), f(z)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def delta_e(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """CIE76 DeltaE between two sRGB tuples (0..1 channels)."""
    lab_a = _rgb_to_lab(a)
    lab_b = _rgb_to_lab(b)
    return sum((x - y) ** 2 for x, y in zip(lab_a, lab_b, strict=True)) ** 0.5


def _srgb_ints(rgb: tuple[float, float, float]) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(c * 255))) for c in rgb)  # type: ignore[return-value]


def _is_standard_space(space: str) -> bool:
    return space in ("DeviceRGB", "DeviceGray", "DeviceCMYK")


def _initial_srgb(
    color: ColorValue | None, color_int: int
) -> tuple[tuple[float, float, float], str]:
    """Best first guess of the rendered sRGB value.

    Returns (rgb floats, source) where source is "stream" for trustworthy
    device-space values, "mupdf" for MuPDF's own conversion and
    "assumed" for guessed colour spaces that the pixel self-check should fix.
    """
    if color is not None:
        space = color.space
        components = color.components
        if space == "DeviceRGB" and len(components) >= 3:
            return (tuple(float(c) for c in components[:3]), "stream")  # type: ignore[return-value]
        if space == "DeviceGray" and len(components) >= 1:
            g = float(components[0])
            return ((g, g, g), "stream")
        if space == "DeviceCMYK" and len(components) >= 4:
            return (_naive_cmyk_to_rgb(components), "stream")
        # ICCBased / Separation / DeviceN / Indexed / Pattern
        if color.n == 3 and len(components) >= 3:
            if color_int >= 0:
                return (_int_to_rgb(color_int), "mupdf")
            return (tuple(float(c) for c in components[:3]), "assumed")  # type: ignore[return-value]
        if color.n == 4 and len(components) >= 4:
            if color_int >= 0:
                return (_int_to_rgb(color_int), "mupdf")
            return (_naive_cmyk_to_rgb(components), "assumed")
        if color.n == 1 and len(components) >= 1 and color_int >= 0:
            return (_int_to_rgb(color_int), "mupdf")
    if color_int >= 0:
        return (_int_to_rgb(color_int), "mupdf")
    return ((0.0, 0.0, 0.0), "assumed")


def _int_to_rgb(value: int) -> tuple[float, float, float]:
    return (
        ((value >> 16) & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        (value & 0xFF) / 255.0,
    )


def _needs_self_check(color: ColorValue | None) -> bool:
    if color is None:
        return True
    return not _is_standard_space(color.space)


def _blend_alpha(
    rgb: tuple[float, float, float], alpha: float
) -> tuple[float, float, float]:
    if alpha >= 0.999:
        return rgb
    a = max(0.0, min(1.0, alpha))
    return tuple(c * a + (1.0 - a) for c in rgb)  # type: ignore[return-value]


def _ink_from_pixels(
    pixels: list[tuple[int, int, int]],
) -> tuple[float, float, float] | None:
    """Mean colour of the foreground pixels in a sampled region."""
    if len(pixels) < 8:
        return None
    buckets = Counter(
        ((r >> 3) << 6) | ((g >> 3) << 3) | (b >> 3) for r, g, b in pixels
    )
    background_key = buckets.most_common(1)[0][0]
    bg = (
        (((background_key >> 6) & 0x7) << 3) + 4,
        (((background_key >> 3) & 0x7) << 3) + 4,
        ((background_key & 0x7) << 3) + 4,
    )
    bg_f = (bg[0] / 255.0, bg[1] / 255.0, bg[2] / 255.0)
    ranked = sorted(
        pixels,
        key=lambda px: (
            (px[0] / 255.0 - bg_f[0]) ** 2
            + (px[1] / 255.0 - bg_f[1]) ** 2
            + (px[2] / 255.0 - bg_f[2]) ** 2
        ),
        reverse=True,
    )
    take = max(4, min(64, len(pixels) // 10))
    ink = ranked[:take]
    return (
        sum(px[0] for px in ink) / (255.0 * len(ink)),
        sum(px[1] for px in ink) / (255.0 * len(ink)),
        sum(px[2] for px in ink) / (255.0 * len(ink)),
    )


def _lerp(
    a: tuple[float, float, float], b: tuple[float, float, float], t: float
) -> tuple[float, float, float]:
    return tuple(x + (y - x) * t for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# profile data structures
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Field:
    """One reported value with its confidence and origin."""

    value: Any
    confidence: float
    origin: str

    def to_dict(self) -> dict[str, Any]:
        value = self.value
        if isinstance(value, float):
            value = round(value, 2)
        elif isinstance(value, tuple):
            value = list(value)
        return {
            "value": value,
            "confidence": round(self.confidence, 3),
            "origin": self.origin,
        }


@dataclass(frozen=True)
class StyleGroup:
    """A cluster of spans that share one detected style."""

    id: int
    font_family: str
    weight: int
    italic: bool
    size: float
    color_srgb: tuple[int, int, int]
    alignment: str
    decorations: tuple[str, ...]
    span_ids: tuple[int, ...]
    char_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "font_family": self.font_family,
            "weight": self.weight,
            "italic": self.italic,
            "size": round(self.size, 2),
            "color_srgb": list(self.color_srgb),
            "alignment": self.alignment,
            "decorations": list(self.decorations),
            "span_ids": list(self.span_ids),
            "char_count": self.char_count,
        }


@dataclass(frozen=True)
class Preset:
    """One of the six always-emitted document presets."""

    name: str
    found: bool
    font_family: str
    weight: int
    italic: bool
    size: float
    line_height: float
    color_srgb: tuple[int, int, int]
    alignment: str
    source_group: int | None
    confidence: float
    origin: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "found": self.found,
            "font_family": self.font_family,
            "weight": self.weight,
            "italic": self.italic,
            "size": round(self.size, 2),
            "line_height": round(self.line_height, 2),
            "color_srgb": list(self.color_srgb),
            "alignment": self.alignment,
            "source_group": self.source_group,
            "confidence": round(self.confidence, 3),
            "origin": self.origin,
        }


@dataclass
class StyleProfile:
    """Full detection result for one page."""

    page_index: int
    has_text_layer: bool
    font_family: Field
    weight: Field
    italic: Field
    size: Field
    line_height: Field
    color_srgb: Field
    alignment: Field
    decorations: Field
    groups: list[StyleGroup] = field(default_factory=list)
    presets: list[Preset] = field(default_factory=list)
    detect_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def preset(self, name: str) -> Preset | None:
        for preset in self.presets:
            if preset.name == name:
                return preset
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "has_text_layer": self.has_text_layer,
            "fields": {
                "font_family": self.font_family.to_dict(),
                "weight": self.weight.to_dict(),
                "italic": self.italic.to_dict(),
                "size": self.size.to_dict(),
                "line_height": self.line_height.to_dict(),
                "color_srgb": self.color_srgb.to_dict(),
                "alignment": self.alignment.to_dict(),
                "decorations": self.decorations.to_dict(),
            },
            "presets": [p.to_dict() for p in self.presets],
            "groups": [g.to_dict() for g in self.groups],
            "detect_ms": round(self.detect_ms, 2),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
@dataclass
class _SpanStyle:
    span: TextSpan
    family: str
    weight: int
    italic: bool
    font_source: str  # os2 | cff | type1 | name | base14 | mupdf
    size: float
    color: tuple[float, float, float]
    color_source: str  # stream | mupdf | assumed | adjusted
    color_confidence: float
    alignment: str
    alignment_confidence: float
    decorations: set[str]


def _content_bounds(page: PageModel) -> tuple[float, float]:
    xs0 = [span.bbox[0] for span in page.spans]
    xs1 = [span.bbox[2] for span in page.spans]
    if not xs0:
        return (0.0, 1.0)
    return (min(xs0), max(xs1))


def _block_alignment(
    block: TextBlock, content: tuple[float, float]
) -> tuple[str, float]:
    lines = block.lines
    if not lines:
        return (ALIGN_LEFT, 0.3)
    x0s = [line.bbox[0] for line in lines]
    x1s = [line.bbox[2] for line in lines]
    centers = [(line.bbox[0] + line.bbox[2]) / 2.0 for line in lines]
    if len(lines) == 1:
        line = lines[0]
        content_x0, content_x1 = content
        width = content_x1 - content_x0
        center = centers[0]
        content_center = (content_x0 + content_x1) / 2.0
        if width > 10 and abs(center - content_center) <= 2.0 and (
            line.bbox[2] - line.bbox[0]
        ) <= width - 4:
            return (ALIGN_CENTER, 0.5)
        if width > 10 and abs(x1s[0] - content_x1) <= 2.0 and x0s[0] > content_x0 + 4:
            return (ALIGN_RIGHT, 0.5)
        return (ALIGN_LEFT, 0.5)

    flush_left = max(x0s) - min(x0s) <= _ALIGN_TOL
    flush_right = max(x1s) - min(x1s) <= _ALIGN_TOL
    mean_center = sum(centers) / len(centers)
    centered = all(abs(c - mean_center) <= _ALIGN_TOL for c in centers)
    content_x0, content_x1 = content
    touches_content = (
        min(x0s) <= content_x0 + 2.0 and max(x1s) >= content_x1 - 2.0
    )
    if flush_left and flush_right and touches_content and len(lines) >= 3:
        return (ALIGN_JUSTIFY, 0.85)
    if flush_right and not flush_left:
        return (ALIGN_RIGHT, 0.9)
    if flush_left and not flush_right:
        return (ALIGN_LEFT, 0.9)
    if centered:
        return (ALIGN_CENTER, 0.9)
    if centered or flush_left:
        return (ALIGN_LEFT, 0.4)
    return (ALIGN_LEFT, 0.4)


def _line_height(page: PageModel) -> tuple[float, float, str]:
    deltas: list[float] = []
    for block in page.blocks:
        if len(block.lines) < 2:
            continue
        origins = sorted(line.origin[1] for line in block.lines)
        for previous, current in pairwise(origins):
            delta = current - previous
            if delta > 1.0:
                deltas.append(delta)
    if not deltas:
        return (0.0, 0.0, ORIGIN_INHERITED)
    med = median(deltas)
    spread = max(abs(d - med) for d in deltas) / med if med else 1.0
    confidence = 0.9 if spread <= 0.15 else 0.7
    return (med, confidence, ORIGIN_DETECTED)


def _span_style(
    page: PageModel,
    span: TextSpan,
    programs: FontPrograms,
    alignment: str,
    alignment_confidence: float,
) -> _SpanStyle:
    first = span.chars[0] if span.chars else None
    cs = page.char_style(first) if first is not None else None
    size = span.size_mupdf
    family = family_from_base(span.font_mupdf)
    weight = 400
    italic = False
    font_source = "mupdf"
    color_source = "mupdf"
    color_confidence = 0.5
    color = _int_to_rgb(span.color_int) if span.color_int >= 0 else (0.0, 0.0, 0.0)

    if cs is not None and cs.source == "stream" and cs.font is not None:
        size = cs.size
        program = programs.get(cs.font)
        family = program.family
        weight = program.weight
        italic = program.italic
        font_source = program.source
        color, color_source = _initial_srgb(cs.fill, span.color_int)
        if color_source == "stream":
            color_confidence = 0.95
        elif color_source == "mupdf":
            color_confidence = 0.7
        else:
            color_confidence = 0.55
        color = _blend_alpha(color, cs.fill_alpha)
    else:
        # extraction fallback: MuPDF's own flags
        if span.flags & 2:
            italic = True
        if span.flags & 16:
            weight = 700
        if span.flags & 8:
            pass  # monospaced; family already carries the name
        guessed = weight_from_base(span.font_mupdf)
        if guessed is not None and not (span.flags & 16):
            weight = guessed
    return _SpanStyle(
        span=span,
        family=family,
        weight=weight,
        italic=italic,
        font_source=font_source,
        size=size,
        color=color,
        color_source=color_source,
        color_confidence=color_confidence,
        alignment=alignment,
        alignment_confidence=alignment_confidence,
        decorations=set(),
    )


def _self_check_color(
    style: _SpanStyle,
    page: PageModel,
    sampler: PixelSampler,
    needs_check: bool,
) -> None:
    if not needs_check:
        return
    if style.color_source == "stream":
        return
    x0, y0, x1, y1 = style.span.bbox
    vx0, vy0 = page.to_view(x0, y0)
    vx1, vy1 = page.to_view(x1, y1)
    view_rect = (min(vx0, vx1), min(vy0, vy1), max(vx0, vx1), max(vy0, vy1))
    if view_rect[2] - view_rect[0] < 1 or view_rect[3] - view_rect[1] < 1:
        return
    try:
        pixels = sampler(*view_rect)
    except Exception:
        return
    measured = _ink_from_pixels(pixels)
    if measured is None:
        return
    current = style.color
    verified = False
    for _ in range(_COLOR_MAX_PASSES):
        if delta_e(current, measured) <= _COLOR_TOLERANCE:
            verified = True
            break
        current = _lerp(current, measured, 0.5)
    style.color = current
    style.color_source = "adjusted"
    style.color_confidence = 0.85 if verified else 0.6


def _detect_decorations(
    page: PageModel,
    styles: list[_SpanStyle],
    links: list[tuple[float, float, float, float]],
) -> None:
    # painted horizontal rules under / through text
    for style in styles:
        span = style.span
        line = span.line
        if line is None:
            continue
        baseline = line.origin[1]
        size = style.size
        if size <= 0:
            continue
        text_x0, text_x1 = span.bbox[0], span.bbox[2]
        span_width = text_x1 - text_x0
        if span_width <= 0:
            continue
        for paint in page.paints:
            if paint.kind not in ("fill", "eofill", "stroke", "fillstroke"):
                continue
            bx0, by0, bx1, by1 = paint.bbox
            if bx1 - bx0 < span_width * 0.5:
                continue
            overlap = min(bx1, text_x1) - max(bx0, text_x0)
            if overlap < span_width * 0.5:
                continue
            height = by1 - by0
            if height > max(2.0, size * 0.35):
                continue
            if baseline - size * 0.05 <= by0 and by1 <= baseline + size * 0.45:
                style.decorations.add("underline")
            strike_y = baseline - size * 0.28
            if abs(((by0 + by1) / 2.0) - strike_y) <= size * 0.22:
                style.decorations.add("strikeout")
    # link annotations
    if links:
        for style in styles:
            x0, y0, x1, y1 = style.span.bbox
            for lx0, ly0, lx1, ly1 in links:
                overlap = (min(x1, lx1) - max(x0, lx0)) * (min(y1, ly1) - max(y0, ly0))
                area = (x1 - x0) * (y1 - y0)
                if area > 0 and overlap >= area * 0.5:
                    style.decorations.add("link")
                    break


def _cluster(styles: list[_SpanStyle]) -> list[tuple[tuple[Any, ...], list[_SpanStyle]]]:
    buckets: dict[tuple[Any, ...], list[_SpanStyle]] = {}
    for style in styles:
        key = (
            style.family,
            style.weight,
            style.italic,
            round(style.size, 1),
            tuple(_srgb_ints(style.color)),
            style.alignment,
            tuple(sorted(style.decorations)),
        )
        buckets.setdefault(key, []).append(style)
    # deterministic: sort by char count desc, then key asc
    return sorted(
        buckets.items(),
        key=lambda item: (-sum(len(s.span.chars) for s in item[1]), item[0]),
    )


def _blocks_for(styles: list[_SpanStyle]) -> list[TextBlock]:
    seen: set[int] = set()
    blocks: list[TextBlock] = []
    for style in styles:
        block = style.span.line.block if style.span.line else None
        if block is None:
            continue
        key = id(block)
        if key not in seen:
            seen.add(key)
            blocks.append(block)
    return blocks


def _preset_line_height(
    styles: list[_SpanStyle], page_line_height: float, size: float
) -> float:
    deltas: list[float] = []
    for block in _blocks_for(styles):
        if len(block.lines) < 2:
            continue
        origins = sorted(line.origin[1] for line in block.lines)
        for previous, current in pairwise(origins):
            if current - previous > 1.0:
                deltas.append(current - previous)
    if deltas:
        return float(median(deltas))
    if page_line_height > 0:
        return page_line_height
    return size * 1.2


def _make_preset(
    name: str,
    group: StyleGroup | None,
    styles: list[_SpanStyle],
    page_line_height: float,
    body_family: str,
    body_color: tuple[int, int, int],
    defaults: tuple[str, int, bool, float],
) -> Preset:
    weight_default, italic_default, size_default = defaults[1:]
    if group is not None:
        line_height = _preset_line_height(styles, page_line_height, group.size)
        return Preset(
            name=name,
            found=True,
            font_family=group.font_family,
            weight=group.weight,
            italic=group.italic,
            size=group.size,
            line_height=line_height,
            color_srgb=group.color_srgb,
            alignment=group.alignment,
            source_group=group.id,
            confidence=0.85,
            origin=ORIGIN_DETECTED,
        )
    size = size_default
    return Preset(
        name=name,
        found=False,
        font_family=body_family,
        weight=weight_default,
        italic=italic_default,
        size=size,
        line_height=page_line_height if page_line_height > 0 else size * 1.2,
        color_srgb=body_color,
        alignment=ALIGN_LEFT,
        source_group=None,
        confidence=0.3,
        origin=ORIGIN_INHERITED,
    )


def _empty_profile(page_index: int, reason: str) -> StyleProfile:
    def inherited(value: Any) -> Field:
        return Field(value, 0.3, ORIGIN_INHERITED)

    presets = []
    for name, weight, italic, size in (
        ("Body", 400, False, 10.0),
        ("H1", 700, False, 24.0),
        ("H2", 700, False, 16.0),
        ("H3", 700, False, 13.0),
        ("Caption", 400, False, 8.0),
        ("Footer", 400, False, 9.0),
    ):
        presets.append(
            Preset(
                name=name,
                found=False,
                font_family="Helvetica",
                weight=weight,
                italic=italic,
                size=size,
                line_height=size * 1.2,
                color_srgb=(0, 0, 0),
                alignment=ALIGN_LEFT,
                source_group=None,
                confidence=0.3,
                origin=ORIGIN_INHERITED,
            )
        )
    return StyleProfile(
        page_index=page_index,
        has_text_layer=False,
        font_family=inherited("Helvetica"),
        weight=inherited(400),
        italic=inherited(False),
        size=inherited(10.0),
        line_height=inherited(12.0),
        color_srgb=inherited((0, 0, 0)),
        alignment=inherited(ALIGN_LEFT),
        decorations=inherited(()),
        groups=[],
        presets=presets,
        warnings=[reason],
    )


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def detect_style(
    page: PageModel,
    programs: FontPrograms,
    *,
    links: list[tuple[float, float, float, float]] | None = None,
    sampler: PixelSampler | None = None,
) -> StyleProfile:
    """Run the full detection pipeline for one page."""
    started = time.perf_counter()
    if not page.spans:
        profile = _empty_profile(page.page_index, "page has no text layer")
        profile.detect_ms = (time.perf_counter() - started) * 1000.0
        return profile

    content = _content_bounds(page)
    # step 3a: alignment per block
    block_alignment: dict[int, tuple[str, float]] = {}
    for block in page.blocks:
        block_alignment[id(block)] = _block_alignment(block, content)

    # step 3b: per-span measurements
    styles: list[_SpanStyle] = []
    for span in page.spans:
        owner = span.line.block if span.line else None
        align, align_conf = (
            block_alignment.get(id(owner), (ALIGN_LEFT, 0.4))
            if owner is not None
            else (ALIGN_LEFT, 0.4)
        )
        styles.append(
            _span_style(page, span, programs, align, align_conf)
        )

    # step 4: pixel self-check for non-device colour spaces
    if sampler is not None:
        for style in styles:
            first = style.span.chars[0] if style.span.chars else None
            cs = page.char_style(first) if first is not None else None
            needs = cs is None or _needs_self_check(cs.fill)
            _self_check_color(style, page, sampler, needs)

    # step 5: decorations
    _detect_decorations(page, styles, links or [])

    # step 6: cluster
    clustered = _cluster(styles)
    groups: list[StyleGroup] = []
    group_styles: dict[int, list[_SpanStyle]] = {}
    for index, (key, members) in enumerate(clustered):
        family, weight, italic, size, color, alignment, decorations = key
        group = StyleGroup(
            id=index,
            font_family=family,
            weight=weight,
            italic=italic,
            size=float(size),
            color_srgb=color,
            alignment=alignment,
            decorations=decorations,
            span_ids=tuple(sorted(s.span.id for s in members)),
            char_count=sum(len(s.span.chars) for s in members),
        )
        groups.append(group)
        group_styles[group.id] = members

    total_chars = sum(len(s.span.chars) for s in styles) or 1

    # line height (page level)
    lh_value, lh_confidence, lh_origin = _line_height(page)
    if lh_value <= 0 and groups:
        lh_value = groups[0].size * 1.2
        lh_confidence = 0.4
        lh_origin = ORIGIN_INHERITED

    # step 6: presets (from clusters)
    body = groups[0] if groups else None
    body_styles = group_styles.get(body.id, []) if body else []
    body_color = body.color_srgb if body else (0, 0, 0)
    body_family = body.font_family if body else "Helvetica"
    body_size = body.size if body else 10.0

    preset_defaults: dict[str, tuple[str, int, bool, float]] = {
        "H1": (body_family, 700, False, 24.0),
        "H2": (body_family, 700, False, 16.0),
        "H3": (body_family, 700, False, 13.0),
        "Caption": (body_family, 400, False, 8.0),
        "Footer": (body_family, 400, False, 9.0),
    }

    # heading tiers: distinct sizes above body size
    tiers: dict[float, StyleGroup] = {}
    for group in groups:
        if group.size < body_size + 0.75:
            continue
        tier_key = round(group.size * 2.0) / 2.0
        current = tiers.get(tier_key)
        if current is None or group.char_count > current.char_count:
            tiers[tier_key] = group
    tier_list = [tiers[key] for key in sorted(tiers, reverse=True)]

    # caption: smallest cluster below body size
    caption_group: StyleGroup | None = None
    for group in reversed(groups):
        if group.size <= body_size - 0.75 and (
            caption_group is None or group.size < caption_group.size
        ):
            caption_group = group

    # footer/header band clusters
    page_height = page.size_unrot[1]
    band_styles = [
        s
        for s in styles
        if s.span.bbox[1] >= page_height * 0.90 or s.span.bbox[3] <= page_height * 0.10
    ]
    footer_group: StyleGroup | None = None
    if band_styles:
        by_group: dict[int, list[_SpanStyle]] = {}
        for style in band_styles:
            # find its group id
            for group in groups:
                if style.span.id in group.span_ids:
                    by_group.setdefault(group.id, []).append(style)
                    break
        if by_group:
            best = max(by_group.items(), key=lambda item: sum(len(s.span.chars) for s in item[1]))
            footer_group = next(g for g in groups if g.id == best[0])

    preset_specs: list[tuple[str, StyleGroup | None, list[_SpanStyle]]] = [
        ("Body", body, body_styles),
        ("H1", tier_list[0] if len(tier_list) > 0 else None, []),
        ("H2", tier_list[1] if len(tier_list) > 1 else None, []),
        ("H3", tier_list[2] if len(tier_list) > 2 else None, []),
        ("Caption", caption_group, []),
        ("Footer", footer_group, []),
    ]
    presets: list[Preset] = []
    for name, spec_group, members in preset_specs:
        if spec_group is not None:
            members = group_styles.get(spec_group.id, [])
        defaults = preset_defaults.get(name, (body_family, 400, False, body_size))
        presets.append(
            _make_preset(
                name, spec_group, members, lh_value, body_family, body_color, defaults
            )
        )

    # step 7: page-level fields
    body_share = (body.char_count / total_chars) if body else 0.0
    base_conf = 0.5 + 0.5 * body_share
    font_conf_by_source = {
        "os2": 0.95,
        "cff": 0.95,
        "type1": 0.85,
        "base14": 0.8,
        "name": 0.75,
        "mupdf": 0.6,
    }
    body_font_conf = 0.5
    if body_styles:
        body_font_conf = font_conf_by_source.get(
            body_styles[0].font_source, 0.6
        )
    family_field = Field(
        body.font_family if body else "Helvetica",
        min(base_conf, body_font_conf) if body else 0.3,
        ORIGIN_DETECTED if body else ORIGIN_INHERITED,
    )
    weight_field = Field(
        body.weight if body else 400,
        min(base_conf, body_font_conf) if body else 0.3,
        ORIGIN_DETECTED if body else ORIGIN_INHERITED,
    )
    italic_field = Field(
        body.italic if body else False,
        min(base_conf, body_font_conf) if body else 0.3,
        ORIGIN_DETECTED if body else ORIGIN_INHERITED,
    )
    size_field = Field(
        body.size if body else 10.0,
        0.5 + 0.5 * body_share if body else 0.3,
        ORIGIN_DETECTED if body else ORIGIN_INHERITED,
    )
    color_field = Field(
        body.color_srgb if body else (0, 0, 0),
        0.5 + 0.4 * body_share if body else 0.3,
        ORIGIN_DETECTED if body else ORIGIN_INHERITED,
    )

    align_votes: Counter[str] = Counter()
    align_conf_sum: dict[str, float] = {}
    for style in styles:
        votes = len(style.span.chars)
        align_votes[style.alignment] += votes
        align_conf_sum[style.alignment] = (
            align_conf_sum.get(style.alignment, 0.0) + votes * style.alignment_confidence
        )
    if align_votes:
        winner = align_votes.most_common(1)[0][0]
        share = align_votes[winner] / total_chars
        mean_conf = align_conf_sum[winner] / align_votes[winner]
        alignment_field = Field(
            winner,
            max(0.4, min(0.95, mean_conf * (0.7 + 0.4 * share))),
            ORIGIN_DETECTED,
        )
    else:
        alignment_field = Field(ALIGN_LEFT, 0.3, ORIGIN_INHERITED)

    line_height_field = Field(
        lh_value if lh_value > 0 else 12.0,
        lh_confidence if lh_value > 0 else 0.3,
        lh_origin,
    )

    decoration_union: tuple[str, ...] = tuple(
        sorted({d for style in styles for d in style.decorations})
    )
    decorations_field = Field(decoration_union, 1.0, ORIGIN_DETECTED)

    profile = StyleProfile(
        page_index=page.page_index,
        has_text_layer=True,
        font_family=family_field,
        weight=weight_field,
        italic=italic_field,
        size=size_field,
        line_height=line_height_field,
        color_srgb=color_field,
        alignment=alignment_field,
        decorations=decorations_field,
        groups=groups,
        presets=presets,
        warnings=[],
    )
    profile.detect_ms = (time.perf_counter() - started) * 1000.0
    return profile

def detect_span_style(
    page: PageModel,
    span: TextSpan,
    programs: FontPrograms,
    *,
    links: list[tuple[float, float, float, float]] | None = None,
    sampler: PixelSampler | None = None,
) -> StyleProfile:
    """Detect the style of exactly one span.

    This is the input the M3 edit engine consumes: same pipeline steps as
    :func:`detect_style` (geometry, font program, original colour space,
    alignment, decorations, pixel self-check) scoped to a single span so the
    replacement text inherits exactly what is on the page.
    """
    started = time.perf_counter()
    if not span.chars:
        return _empty_profile(page.page_index, "span has no characters")

    owner = span.line.block if span.line is not None else None
    if owner is not None:
        align, align_conf = _block_alignment(owner, _content_bounds(page))
    else:
        align, align_conf = ALIGN_LEFT, 0.4
    style = _span_style(page, span, programs, align, align_conf)

    if sampler is not None:
        first = span.chars[0]
        cs = page.char_style(first)
        needs = cs is None or _needs_self_check(cs.fill)
        _self_check_color(style, page, sampler, needs)

    _detect_decorations(page, [style], links or [])

    font_conf = {
        "os2": 0.95,
        "cff": 0.95,
        "type1": 0.85,
        "base14": 0.8,
        "name": 0.75,
        "mupdf": 0.6,
    }.get(style.font_source, 0.6)
    size_conf = 0.9 if style.font_source != "mupdf" else 0.7
    lh_value, lh_conf, lh_origin = _line_height(page)
    if lh_value <= 0:
        lh_value = style.size * 1.2
        lh_conf, lh_origin = 0.4, ORIGIN_INHERITED

    decorations = tuple(sorted(style.decorations))
    group = StyleGroup(
        id=0,
        font_family=style.family,
        weight=style.weight,
        italic=style.italic,
        size=style.size,
        color_srgb=_srgb_ints(style.color),
        alignment=style.alignment,
        decorations=decorations,
        span_ids=(span.id,),
        char_count=len(span.chars),
    )
    profile = StyleProfile(
        page_index=page.page_index,
        has_text_layer=True,
        font_family=Field(style.family, font_conf, ORIGIN_DETECTED),
        weight=Field(style.weight, font_conf, ORIGIN_DETECTED),
        italic=Field(style.italic, font_conf, ORIGIN_DETECTED),
        size=Field(style.size, size_conf, ORIGIN_DETECTED),
        line_height=Field(lh_value, lh_conf, lh_origin),
        color_srgb=Field(_srgb_ints(style.color), style.color_confidence, ORIGIN_DETECTED),
        alignment=Field(
            style.alignment, style.alignment_confidence, ORIGIN_DETECTED
        ),
        decorations=Field(
            decorations, 1.0 if decorations else 0.5, ORIGIN_DETECTED
        ),
        groups=[group],
        presets=[],
        warnings=[],
    )
    profile.detect_ms = (time.perf_counter() - started) * 1000.0
    return profile
