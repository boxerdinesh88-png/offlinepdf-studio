"""Layout reconstruction: OCR boxes -> editable text lines.

Input boxes live in the *straightened* (deskewed) pixel frame, where text
lines are axis-aligned.  Segments of one visual line are merged, then each
line gets a font size and baseline (calibrated against PP-OCR box geometry),
text and background colours sampled from the original pixels, a bold guess
from ink density, and a font family by script.  Finally everything is mapped
into view-space points (PyMuPDF ``page.rect``), the space the editor uses.

Paragraph grouping is deliberately left to the regular text model: lines are
written as real text, and the editor's block detection groups them exactly
like any other PDF text.
"""

from __future__ import annotations

import dataclasses
import math
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from .preprocess import Mapping, map_point

__all__ = ["OcrLine", "Segment", "build_lines", "font_for_text"]

Quad = tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]
RGB = tuple[float, float, float]

# Calibrated on RapidOCR/PP-OCRv3 boxes (Liberation Sans/Serif, 5-44 pt):
_SIZE_PER_BOX_HEIGHT = 1.0 / 1.06
_BASELINE_ABOVE_BOTTOM = 0.24  # x size
_MERGE_GAP = 1.2  # x line height
# Stroke thickness / font size in pixels, calibrated on Liberation Sans/Serif/
# Mono and Carlito at 8-28 pt, 200-300 DPI JPEG: regular <= 0.087 (serif
# <= 0.079), bold >= 0.107 (serif >= 0.099).
_BOLD_STROKE = {"LiberationSerif": 0.089}
_BOLD_STROKE_DEFAULT = 0.097
# Shear (dx/dy) that makes glyphs most upright: italics 0.16-0.20, roman 0.0.
_ITALIC_SLANT = 0.10
_UNIFORM_BG_STD = 8.0
_MATCH_MIN_CHARS = 5
_FACE_MARGIN = 0.02  # tie-break towards sans (unknown fonts are handled by _FAMILIAR_OVERLAP)
_VERTICAL_BOX = 1.3  # boxes taller than wide by this factor hold vertical text
# Shape similarity = normalised cross-correlation of slightly blurred glyph
# masks (italics compared de-sheared).  Below this the printed font is not
# one we bundle (e.g. Lato in a chart) and face/weight guesses are noise
# (measured: known faces 0.70-0.87, unknown fonts <= 0.45), so a neutral sans
# is used instead.
_FAMILIAR_SIMILARITY = 0.60
_FAMILIAR_LETTERS = 6
_FAMILIAR_MIN_PX = 32.0  # smaller glyphs are too blurred to tell faces apart
_BOLD_VS_PAGE = 1.15  # known fonts: bold strokes this much thicker than the page median
# For unknown fonts only large text can be judged bold: anti-aliasing makes
# every stroke of small text look thick.
_UNKNOWN_BOLD_MIN_PX = 40.0
_UNKNOWN_BOLD_STROKE = 0.10


@dataclass(frozen=True)
class Segment:
    """One recognised box in the straightened pixel frame."""

    text: str
    confidence: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0


@dataclass(frozen=True)
class OcrLine:
    """One editable line, in view-space points."""

    text: str
    confidence: float
    quad: Quad  # top-left, top-right, bottom-right, bottom-left
    origin: tuple[float, float]  # baseline start
    angle: float  # clockwise degrees on screen
    size: float
    width: float  # along the baseline
    fg: RGB
    bg: RGB
    bg_uniform: bool
    bold: bool = False
    italic: bool = False
    font: str = "LiberationSans"
    #: tight box around the line's own ink - what gets covered when edited
    cover: Quad | None = None
    #: per-word (text, confidence, quad) for lines that were uncertain overall
    words: tuple[tuple[str, float, Quad], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "quad": [list(p) for p in self.quad],
            "origin": list(self.origin),
            "angle": self.angle,
            "size": self.size,
            "width": self.width,
            "fg": list(self.fg),
            "bg": list(self.bg),
            "bg_uniform": self.bg_uniform,
            "bold": self.bold,
            "italic": self.italic,
            "font": self.font,
            "cover": [list(p) for p in self.cover] if self.cover else None,
            "words": [[w, c, [list(p) for p in q]] for w, c, q in self.words],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OcrLine:
        quad = tuple(tuple(float(v) for v in p) for p in data["quad"])
        cover = data.get("cover")
        return cls(
            text=str(data["text"]),
            confidence=float(data["confidence"]),
            quad=quad,  # type: ignore[arg-type]
            origin=(float(data["origin"][0]), float(data["origin"][1])),
            angle=float(data["angle"]),
            size=float(data["size"]),
            width=float(data["width"]),
            fg=tuple(float(v) for v in data["fg"]),  # type: ignore[arg-type]
            bg=tuple(float(v) for v in data["bg"]),  # type: ignore[arg-type]
            bg_uniform=bool(data["bg_uniform"]),
            bold=bool(data.get("bold", False)),
            italic=bool(data.get("italic", False)),
            font=str(data.get("font", "LiberationSans")),
            cover=tuple(tuple(float(v) for v in p) for p in cover) if cover else None,  # type: ignore[arg-type]
            words=tuple(
                (str(w), float(c), tuple(tuple(float(v) for v in p) for p in q))  # type: ignore[misc]
                for w, c, q in data.get("words") or ()
            ),
        )


def font_for_text(text: str) -> str:
    """Bundled family able to show ``text`` (by dominant script)."""
    counts: dict[str, int] = {}
    for char in text:
        code = ord(char)
        if 0x0900 <= code <= 0x097F:
            key = "NotoSansDevanagari"
        elif 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F:
            key = "NotoSansArabic"
        elif 0x0590 <= code <= 0x05FF:
            key = "NotoSansHebrew"
        elif unicodedata.east_asian_width(char) in ("W", "F"):
            key = "NotoSansSC"
        else:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "LiberationSans"
    return max(counts, key=lambda k: counts[k])


def _ink_mask(rgb: np.ndarray, seg: Segment) -> np.ndarray | None:
    """Binary text mask of the segment (text = True), or None if empty."""
    h, w = rgb.shape[:2]
    x0, x1 = int(max(0, seg.x0)), int(min(w, seg.x1))
    y0, y1 = int(max(0, seg.y0)), int(min(h, seg.y1))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    luma = rgb[y0:y1, x0:x1].astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    mask = luma < (luma.min() + luma.max()) / 2.0
    mask = ~mask if mask.mean() > 0.5 else mask
    return strip_rules(mask)


def is_vertical(seg: Segment) -> bool:
    """Box much taller than wide: a rotated (vertical) line, not a horizontal one."""
    return len(seg.text.strip()) >= 2 and seg.height > _VERTICAL_BOX * (seg.x1 - seg.x0)


def strip_rules(mask: np.ndarray, line_height: float | None = None) -> np.ndarray:
    """Remove ruling lines (axes, borders, ticks, underlines) from a text mask.

    A ruling line crosses the whole box, which no glyph stroke does: a
    horizontal run spanning the box width (and over ~1.8x the line height),
    or a vertical one spanning its height (and longer than any glyph), is a
    line that only happens to sit in the text box (chart ticks, table cells,
    form fields).
    """
    import cv2

    h = float(line_height or mask.shape[0])
    if mask.size == 0 or h < 4:
        return mask
    m = mask.astype(np.uint8)
    # Rules run past the text into the margins: a horizontal run must also
    # span (nearly) the whole box, or a photo's blurred baseline would count.
    h_len = max(4, round(1.8 * h), round(0.95 * mask.shape[1]))
    # Vertical rules span the whole box; with a known line height they must
    # also be longer than any glyph stroke ("/", "l", "|" reach ~1.0 x size).
    v_len = max(4, round(0.92 * mask.shape[0]))
    if line_height:
        v_len = max(v_len, round(1.15 * line_height))
    horizontal = cv2.morphologyEx(
        m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (h_len, 1))
    )
    vertical = cv2.morphologyEx(
        m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_len))
    )
    rules = np.logical_or(horizontal > 0, vertical > 0)
    if not rules.any():
        return mask
    rules = cv2.dilate(rules.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    return mask & ~rules


def _ink_extent(rgb: np.ndarray, seg: Segment) -> tuple[float, float, float, float] | None:
    """Tight ink box of the line itself, in straightened pixels.

    Detector boxes are padded and can reach into neighbouring lines or along
    a ruling line, so split the rows into bands (gaps of a couple of pixels
    allowed) and keep the band holding the most ink: a full line of glyphs
    always outweighs a stray rule or the descenders of the line above.
    """
    mask = _ink_mask(rgb, seg)
    if mask is None or not mask.any():
        return None
    rows = mask.sum(axis=1)
    gap = max(2, int(0.06 * mask.shape[0]))
    bands: list[tuple[int, int]] = []
    start = last = -1
    for y, count in enumerate(rows):
        if count == 0:
            continue
        if start < 0:
            start = y
        elif y - last > gap + 1:
            bands.append((start, last))
            start = y
        last = y
    if start >= 0:
        bands.append((start, last))
    top, bottom = max(bands, key=lambda band: int(rows[band[0] : band[1] + 1].sum()))
    cols = np.nonzero(mask[top : bottom + 1].any(axis=0))[0]
    if cols.size == 0:
        return None
    x0 = max(0.0, seg.x0)
    y0 = max(0.0, seg.y0)
    return (x0 + cols[0], y0 + top, x0 + cols[-1] + 1, y0 + bottom + 1)


def _stroke_ratio(mask: np.ndarray, size_px: float) -> float:
    """Mean stroke thickness relative to the font size (sub-pixel precise)."""
    import cv2

    big = cv2.resize(mask.astype(np.uint8), None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    dist = cv2.distanceTransform(big, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    ridge = (dist >= cv2.dilate(dist, np.ones((3, 3), np.uint8))) & (dist > 1.0)
    if not ridge.any() or size_px <= 0:
        return 0.0
    return float(2.0 * np.mean(dist[ridge])) / (3.0 * size_px)


def _slant(mask: np.ndarray) -> float:
    """Horizontal shear that makes the glyphs most upright (italic > 0)."""
    import cv2

    h, w = mask.shape
    image = mask.astype(np.uint8) * 255
    best, best_score = 0.0, -1.0
    for shear in np.arange(-0.1, 0.42, 0.02):
        matrix = np.array([[1.0, shear, -shear * h / 2.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        sheared = cv2.warpAffine(image, matrix, (w + int(h * 0.5), h))
        columns = sheared.sum(axis=0).astype(np.float64)
        score = float((columns**2).sum())
        if score > best_score:
            best, best_score = float(shear), score
    return best


def _weight_and_slant(
    rgb: np.ndarray, seg: Segment, size_px: float, family: str
) -> tuple[bool, bool]:
    """(bold, italic) for a Latin line from its own pixels."""
    mask = _ink_mask(rgb, seg)
    if mask is None or size_px < 10 or len(seg.text.strip()) < 3:
        return False, False
    rows = np.nonzero(mask.any(axis=1))[0]
    cols = np.nonzero(mask.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return False, False
    mask = mask[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]
    # Too short to identify the font: judge weight like an unknown font.
    bold = size_px >= _UNKNOWN_BOLD_MIN_PX and _stroke_ratio(mask, size_px) >= _UNKNOWN_BOLD_STROKE
    italic = len(seg.text.strip()) >= 4 and _slant(mask) >= _ITALIC_SLANT
    return bold, italic


@dataclass(frozen=True)
class FaceMatch:
    """Best bundled face for a line, plus geometry measured from its glyphs."""

    family: str
    bold: bool
    italic: bool
    size_px: float  # font size that reproduces the line's ink height
    origin: tuple[float, float]  # baseline start, straightened pixels
    width_px: float  # advance width the line occupies
    score: float
    stroke: float = 0.0  # stroke thickness / font size (bold measure)
    known: bool = True  # face recognised (False: neutral sans for an unknown font)


_FACE_FILES = {
    "LiberationSans": "LiberationSans/LiberationSans",
    "LiberationSerif": "LiberationSerif/LiberationSerif",
    "LiberationMono": "LiberationMono/LiberationMono",
    "Carlito": "Carlito/Carlito",
}


def _face_path(family: str, bold: bool, italic: bool) -> str | None:
    from offlinepdf.config import resource_path

    style = ("Bold" if bold else "") + ("Italic" if italic else "") or "Regular"
    path = resource_path("assets", "fonts", *f"{_FACE_FILES[family]}-{style}.ttf".split("/"))
    return str(path) if path.exists() else None


def _render_mask(
    path: str, text: str
) -> tuple[np.ndarray, tuple[float, float, float, float], float]:
    """Ink mask at 64 px, its bbox relative to the baseline origin, and advance."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(path, 64)
    left, top, right, bottom = font.getbbox(text, anchor="ls")
    canvas = Image.new("L", (int(right - left) + 2, int(bottom - top) + 2), 0)
    ImageDraw.Draw(canvas).text((1 - left, 1 - top), text, font=font, fill=255, anchor="ls")
    mask = np.asarray(canvas) > 127
    return mask, (float(left), float(top), float(right), float(bottom)), float(font.getlength(text))


def _deshear(mask: np.ndarray) -> np.ndarray:
    """Undo the glyph slant (italics are compared upright)."""
    import cv2

    shear = _slant(mask)
    if abs(shear) < 0.05:
        return mask
    h, w = mask.shape
    matrix = np.array([[1.0, shear, -shear * h / 2.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    size = (w + int(abs(shear) * h) + 2, h)
    out = cv2.warpAffine(mask.astype(np.uint8) * 255, matrix, size) > 127
    cols = np.nonzero(out.any(axis=0))[0]
    return out[:, cols[0] : cols[-1] + 1] if cols.size else out


def _similarity(a: np.ndarray, b: np.ndarray, sigma: float) -> float:
    """Normalised cross-correlation of blurred masks (tolerates 1-2 px shifts)."""
    import cv2

    fa = np.asarray(cv2.GaussianBlur(a.astype(np.float32), (0, 0), sigma), dtype=np.float64)
    fb = np.asarray(cv2.GaussianBlur(b.astype(np.float32), (0, 0), sigma), dtype=np.float64)
    fa = fa - float(fa.mean())
    fb = fb - float(fb.mean())
    den = float(np.sqrt(float((fa * fa).sum()) * float((fb * fb).sum())))
    return float((fa * fb).sum()) / den if den else 0.0


def _match_face(rgb: np.ndarray, seg: Segment) -> FaceMatch | None:
    """Pick sans/serif/mono/Carlito by rendered shape and proportions.

    Works on the tight ink box of the line: weight comes from stroke
    thickness, italic from the glyph slant, and the face from overlap plus
    aspect ratio (monospace is far wider).  The winning rendering also gives
    the exact font size and baseline, independent of detector padding.
    """
    text = seg.text.strip()
    extent = _ink_extent(rgb, seg)
    if extent is None or len(text) < _MATCH_MIN_CHARS:
        return None
    ex0, ey0, ex1, ey1 = extent
    tight = Segment(text, seg.confidence, ex0, ey0, ex1, ey1)
    target = _ink_mask(rgb, tight)
    if target is None or not target.any():
        return None
    th, tw = target.shape
    italic = len(text) >= 4 and _slant(target) >= _ITALIC_SLANT
    stroke = _stroke_ratio(target, th / 0.94)
    upright_target = _deshear(target) if italic else target
    sigma = max(1.0, upright_target.shape[0] / 30.0)
    best: FaceMatch | None = None
    best_overlap = 0.0
    sans_score: float | None = None
    for family in _FACE_FILES:
        bold = stroke >= _BOLD_STROKE.get(family, _BOLD_STROKE_DEFAULT)
        path = _face_path(family, bold, italic) or _face_path(family, bold, False)
        if path is None:
            continue
        try:
            rendered, (left, top, right, bottom), advance = _render_mask(path, text)
        except OSError:
            continue
        ink_w, ink_h = right - left, bottom - top
        if ink_w < 2 or ink_h < 2:
            continue
        from PIL import Image

        scaled = (
            np.asarray(
                Image.fromarray(rendered.astype(np.uint8) * 255).resize(
                    (tw, th), Image.Resampling.BILINEAR
                )
            )
            > 127
        )
        if italic:
            upright = _deshear(rendered)
            ut_h, ut_w = upright_target.shape
            scaled = (
                np.asarray(
                    Image.fromarray(upright.astype(np.uint8) * 255).resize(
                        (ut_w, ut_h), Image.Resampling.BILINEAR
                    )
                )
                > 127
            )
            overlap = _similarity(scaled, upright_target, sigma)
        else:
            overlap = _similarity(scaled, target, sigma)
        aspect = abs(np.log((ink_w / ink_h) / (tw / th)))
        score = overlap - 0.15 * float(aspect)
        candidate_k = th / ink_h
        candidate = FaceMatch(
            family=family,
            bold=bold,
            italic=italic,
            size_px=64.0 * candidate_k,
            origin=(
                ex0 - left * candidate_k * (tw / (ink_w * candidate_k)),
                ey0 - top * candidate_k,
            ),
            width_px=advance * candidate_k * (tw / (ink_w * candidate_k)),
            score=score,
            stroke=stroke,
        )
        if family == "LiberationSans":
            sans_score = score
            sans_match = candidate
        best_overlap = max(best_overlap, overlap)
        if best is None or score > best.score:
            k = th / ink_h  # rendered px -> target px
            ratio_w = tw / (ink_w * k)  # horizontal scale actually printed
            best = FaceMatch(
                family=family,
                bold=bold,
                italic=italic,
                size_px=64.0 * k,
                origin=(ex0 - left * k * ratio_w, ey0 - top * k),
                width_px=advance * k * ratio_w,
                score=score,
                stroke=stroke,
            )
    if best is None or sans_score is None:
        return best
    letters = sum(char.isalpha() for char in text)
    if (
        best_overlap < _FAMILIAR_SIMILARITY
        or letters < _FAMILIAR_LETTERS
        or sans_match.size_px < _FAMILIAR_MIN_PX
    ):
        # Unknown font (or mostly digits): neutral sans, weight only when sure.
        bold = sans_match.size_px >= _UNKNOWN_BOLD_MIN_PX and stroke >= _UNKNOWN_BOLD_STROKE
        return dataclasses.replace(sans_match, bold=bold, known=False)
    if best.family != "LiberationSans" and best.score - sans_score < _FACE_MARGIN:
        return sans_match  # a non-sans face must win clearly
    return best


def _rule_between(rgb: np.ndarray | None, a: Segment, b: Segment) -> bool:
    """True when a vertical line (table cell border) separates ``a`` and ``b``."""
    if rgb is None:
        return False
    h, w = rgb.shape[:2]
    x0, x1 = int(max(0, min(a.x1, b.x0) - 2)), int(min(w, max(a.x1, b.x0) + 2))
    y0, y1 = int(max(0, min(a.y0, b.y0))), int(min(h, max(a.y1, b.y1)))
    if x1 - x0 < 1 or y1 - y0 < 4:
        return False
    luma = rgb[y0:y1, x0:x1].astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    paper = float(np.percentile(luma, 90))
    dark = luma < paper - 40
    return bool((dark.mean(axis=0) >= 0.85).any())


def _fit_sans(rgb: np.ndarray, seg: Segment) -> FaceMatch | None:
    """Size/baseline for lines too short to identify the face (e.g. "123 K").

    Renders the recognised text in the neutral sans and scales it to the
    line's real ink height, so detector padding or a nearby rule never
    inflates the font size.
    """
    text = seg.text.strip()
    extent = _ink_extent(rgb, seg)
    path = _face_path("LiberationSans", False, False)
    if not text or extent is None or path is None:
        return None
    ex0, ey0, ex1, ey1 = extent
    try:
        _rendered, (left, top, right, bottom), advance = _render_mask(path, text)
    except OSError:
        return None
    ink_w, ink_h = right - left, bottom - top
    if ink_w < 1 or ink_h < 1:
        return None
    k = (ey1 - ey0) / ink_h
    ratio_w = (ex1 - ex0) / (ink_w * k) if ink_w * k > 0 else 1.0
    return FaceMatch(
        family="LiberationSans",
        bold=False,
        italic=False,
        size_px=64.0 * k,
        origin=(ex0 - left * k * ratio_w, ey0 - top * k),
        width_px=advance * k * ratio_w,
        score=0.0,
    )


def _merge(segments: list[Segment], rgb: np.ndarray | None = None) -> list[Segment]:
    """Join segments of the same visual line (detectors split on wide gaps)."""
    rows: list[list[Segment]] = []
    for seg in sorted(segments, key=lambda s: (s.cy, s.x0)):
        for row in rows:
            ref = row[-1]
            same_row = abs(seg.cy - ref.cy) < 0.35 * max(seg.height, ref.height)
            similar = max(seg.height, ref.height) < 2.0 * min(seg.height, ref.height)
            if same_row and similar:
                row.append(seg)
                break
        else:
            rows.append([seg])
    merged: list[Segment] = []
    for row in rows:
        row.sort(key=lambda s: s.x0)
        current = row[0]
        for seg in row[1:]:
            h = max(current.height, seg.height)
            gap = seg.x0 - current.x1
            if -0.3 * h <= gap <= _MERGE_GAP * h and not _rule_between(rgb, current, seg):
                joiner = " " if gap > 0.03 * h and not current.text.endswith(" ") else ""
                total = len(current.text) + len(seg.text) or 1
                confidence = (
                    current.confidence * len(current.text) + seg.confidence * len(seg.text)
                ) / total
                current = Segment(
                    current.text + joiner + seg.text,
                    confidence,
                    min(current.x0, seg.x0),
                    min(current.y0, seg.y0),
                    max(current.x1, seg.x1),
                    max(current.y1, seg.y1),
                )
            else:
                merged.append(current)
                current = seg
        merged.append(current)
    return merged


def _sample(rgb: np.ndarray, seg: Segment) -> tuple[RGB, RGB, bool, float]:
    """(text colour, background colour, uniform background?, ink density)."""
    h, w = rgb.shape[:2]
    pad = 0.08 * seg.height
    x0, x1 = int(max(0, seg.x0 + pad)), int(min(w, seg.x1 - pad))
    y0, y1 = int(max(0, seg.y0 + pad)), int(min(h, seg.y1 - pad))
    crop = rgb[y0:y1, x0:x1].reshape(-1, 3).astype(np.float64)
    if crop.shape[0] < 4:
        return ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0), True, 0.0)
    luma = crop @ np.array([0.299, 0.587, 0.114])
    # Otsu on the luminance histogram.
    hist, edges = np.histogram(luma, bins=64, range=(0.0, 255.0))
    best_t, best_var = 128.0, -1.0
    total = hist.sum()
    centers = (edges[:-1] + edges[1:]) / 2.0
    cum = np.cumsum(hist)
    cum_mean = np.cumsum(hist * centers)
    for i in range(1, 63):
        w0, w1 = cum[i], total - cum[i]
        if w0 == 0 or w1 == 0:
            continue
        m0 = cum_mean[i] / w0
        m1 = (cum_mean[-1] - cum_mean[i]) / w1
        var = w0 * w1 * (m0 - m1) ** 2
        if var > best_var:
            best_var, best_t = var, float(edges[i + 1])
    dark = luma < best_t
    # Text is the minority class (handles light-on-dark too).
    ink = dark if dark.mean() <= 0.5 else ~dark
    if not ink.any() or ink.all():
        ink = dark
    bg_pixels = crop[~ink] if (~ink).any() else crop
    bg = np.median(bg_pixels, axis=0) / 255.0
    if ink.any():
        # Stroke cores only: anti-aliased edges would wash the colour out.
        ink_pixels = crop[ink]
        distance = np.abs(luma[ink] - float(np.median(luma[~ink])) if (~ink).any() else luma[ink])
        core = ink_pixels[distance >= np.percentile(distance, 60)]
        fg = np.median(core if core.size else ink_pixels, axis=0) / 255.0
    else:
        fg = np.zeros(3)
    # Judge uniformity away from anti-aliased glyph edges: keep background
    # pixels on the far side of the midpoint between threshold and median.
    bg_luma = luma[~ink] if (~ink).any() else luma
    middle = (best_t + float(np.median(bg_luma))) / 2.0
    light_bg = np.median(bg_luma) >= best_t
    clear = bg_luma[bg_luma >= middle] if light_bg else bg_luma[bg_luma <= middle]
    uniform = float(np.std(clear if clear.size > 8 else bg_luma)) < _UNIFORM_BG_STD
    return (
        (float(fg[0]), float(fg[1]), float(fg[2])),
        (float(bg[0]), float(bg[1]), float(bg[2])),
        uniform,
        float(ink.mean()),
    )


def build_lines(
    segments: list[Segment],
    straight_rgb: np.ndarray,
    back: Mapping,
    px_per_pt: float,
    *,
    photo: bool = False,
    score_words: Callable[[list[np.ndarray]], list[tuple[str, float]]] | None = None,
) -> list[OcrLine]:
    """Turn straightened-frame segments into view-space :class:`OcrLine` s.

    ``back`` maps straightened pixels onto the original rendering and
    ``px_per_pt`` is the rendering scale (DPI / 72).
    """
    merged = [
        s
        for s in _merge(segments, straight_rgb)
        if s.text.strip()
        and s.height > 1
        # Vertical text read as a horizontal line is garbage; the pipeline
        # re-reads such boxes turned upright (see pipeline._vertical_lines).
        and not is_vertical(s)
    ]
    if not merged:
        return []

    def to_view(x: float, y: float) -> tuple[float, float]:
        ox, oy = map_point(back, x, y)
        return (ox / px_per_pt, oy / px_per_pt)

    samples = [_sample(straight_rgb, seg) for seg in merged]
    matches = [
        _match_face(straight_rgb, seg) if font_for_text(seg.text) == "LiberationSans" else None
        for seg in merged
    ]
    # Bold must stand out from the page's ordinary text (the 30th percentile:
    # bold is the minority), since blur or a heavy house font thickens every
    # stroke.  Photos are too blurred to judge weight from at all.
    strokes = sorted(m.stroke for m in matches if m is not None and m.stroke > 0)
    page_stroke = strokes[int(0.3 * (len(strokes) - 1))] if len(strokes) >= 3 else 0.0
    lines: list[OcrLine] = []
    merged = [_restore_spaces(straight_rgb, seg, score_words) for seg in merged]
    for seg, (fg, bg, uniform, _ink), match in zip(merged, samples, matches, strict=True):
        # Local straightened-pixel -> view-point scale at this line (differs
        # across a photographed page under perspective; constant otherwise).
        mid_x, mid_y = (seg.x0 + seg.x1) / 2.0, (seg.y0 + seg.y1) / 2.0
        here = to_view(mid_x, mid_y)
        below = to_view(mid_x, mid_y + 1.0)
        v_scale = math.hypot(below[0] - here[0], below[1] - here[1]) or 1.0 / px_per_pt
        h_pt = seg.height * v_scale
        size = max(2.0, h_pt * _SIZE_PER_BOX_HEIGHT)
        baseline_px = seg.y1 - _BASELINE_ABOVE_BOTTOM * size / v_scale
        quad = (
            to_view(seg.x0, seg.y0),
            to_view(seg.x1, seg.y0),
            to_view(seg.x1, seg.y1),
            to_view(seg.x0, seg.y1),
        )
        # Per-line slant from its own mapped baseline direction (rotation,
        # skew and perspective all included).
        angle = math.degrees(
            math.atan2(quad[1][1] - quad[0][1], quad[1][0] - quad[0][0])
        )
        text = " ".join(seg.text.split())
        family = font_for_text(text)
        bold = italic = False
        origin_px = (seg.x0, baseline_px)
        width_px = seg.x1 - seg.x0
        if family == "LiberationSans":
            if match is not None:
                family, bold, italic = match.family, match.bold, match.italic
                if match.known and page_stroke and match.stroke < _BOLD_VS_PAGE * page_stroke:
                    bold = False
                if photo and not match.known:
                    bold = False
                size = max(2.0, match.size_px * v_scale)
                origin_px, width_px = match.origin, match.width_px
            else:
                fitted = _fit_sans(straight_rgb, seg)
                if fitted is not None:  # size/baseline from the glyphs, not the box
                    size = max(2.0, fitted.size_px * v_scale)
                    origin_px, width_px = fitted.origin, fitted.width_px
                bold, italic = _weight_and_slant(straight_rgb, seg, size / v_scale, family)
                bold = bold and not photo
        cover = None
        extent = _ink_extent(straight_rgb, seg)
        if extent is not None:
            pad = max(1.5, 0.05 * size * px_per_pt)
            cx0, cy0, cx1, cy1 = extent[0] - pad, extent[1] - pad, extent[2] + pad, extent[3] + pad
            cover = (to_view(cx0, cy0), to_view(cx1, cy0), to_view(cx1, cy1), to_view(cx0, cy1))
        lines.append(
            OcrLine(
                text=text,
                confidence=round(seg.confidence, 4),
                quad=quad,
                origin=to_view(*origin_px),
                angle=round(angle, 3),
                size=round(size, 2),
                width=math.dist(
                    to_view(*origin_px), to_view(origin_px[0] + width_px, origin_px[1])
                ),
                fg=fg,
                bg=bg,
                bg_uniform=uniform,
                bold=bold,
                italic=italic,
                font=family,
                cover=cover,
                words=_word_scores(straight_rgb, seg, to_view, score_words),
            )
        )
    return lines


_WORD_CHECK_BELOW = 0.92  # lines this sure need no per-word review


def _word_ranges(rgb: np.ndarray, seg: Segment) -> list[tuple[int, int]]:
    """Column ranges of the words in a line (split at clear ink gaps)."""
    mask = _ink_mask(rgb, seg)
    if mask is None:
        return []
    columns = mask.any(axis=0)
    gap = max(3, round(0.28 * seg.height))
    ranges: list[tuple[int, int]] = []
    start = None
    blank = 0
    for x, ink in enumerate(columns):
        if ink:
            if start is None:
                start = x
            blank = 0
            end = x
        elif start is not None:
            blank += 1
            if blank >= gap:
                ranges.append((start, end))
                start = None
    if start is not None:
        ranges.append((start, end))
    return ranges


def _restore_spaces(
    rgb: np.ndarray,
    seg: Segment,
    score_words: Callable[[list[np.ndarray]], list[tuple[str, float]]] | None,
) -> Segment:
    """Put back word spaces the recogniser dropped ("Note7731" -> "Note 7731").

    When the ink shows more word gaps than the text has spaces, each gapped
    piece is read on its own; the spaced text is accepted only if it spells
    exactly the same characters, so nothing but spaces can change.
    """
    if score_words is None or len(seg.text) < 4:
        return seg
    ranges = _word_ranges(rgb, seg)
    if len(ranges) <= len(seg.text.split()):
        return seg
    h, w = rgb.shape[:2]
    x_base = max(0.0, seg.x0)
    y0, y1 = int(max(0, seg.y0)), int(min(h, seg.y1))
    pad = max(2, round(0.15 * seg.height))
    crops = [
        np.ascontiguousarray(
            rgb[y0:y1, int(max(0, x_base + a - pad)) : int(min(w, x_base + b + 1 + pad))]
        )
        for a, b in ranges
    ]
    try:
        pieces = [text.strip() for text, _score in score_words(crops)]
    except Exception:  # spacing repair is optional
        return seg
    if not all(pieces):
        return seg
    squash = "".join(seg.text.split())
    if "".join(pieces) != squash:
        return seg
    return dataclasses.replace(seg, text=" ".join(pieces))


def _word_scores(
    rgb: np.ndarray,
    seg: Segment,
    to_view: Callable[[float, float], tuple[float, float]],
    score_words: Callable[[list[np.ndarray]], list[tuple[str, float]]] | None,
) -> tuple[tuple[str, float, Quad], ...]:
    """Per-word confidence for an uncertain line, by re-reading each word."""
    tokens = seg.text.split()
    if score_words is None or seg.confidence >= _WORD_CHECK_BELOW or len(tokens) < 2:
        return ()
    ranges = _word_ranges(rgb, seg)
    if len(ranges) != len(tokens):
        return ()  # can't pair gaps with words reliably: keep the line-level score
    h, w = rgb.shape[:2]
    x_base = max(0.0, seg.x0)
    y0, y1 = int(max(0, seg.y0)), int(min(h, seg.y1))
    pad = max(2, round(0.15 * seg.height))
    crops, boxes = [], []
    for start, end in ranges:
        cx0 = int(max(0, x_base + start - pad))
        cx1 = int(min(w, x_base + end + 1 + pad))
        crops.append(np.ascontiguousarray(rgb[y0:y1, cx0:cx1]))
        boxes.append((cx0, cx1))
    try:
        scored = score_words(crops)
    except Exception:  # word review is optional
        return ()
    out = []
    for token, (_text, score), (cx0, cx1) in zip(tokens, scored, boxes, strict=False):
        quad = (to_view(cx0, y0), to_view(cx1, y0), to_view(cx1, y1), to_view(cx0, y1))
        # A word read alone has little context (short ones score low even when
        # right): blend with the line's score so only real doubts stand out.
        blended = math.sqrt(max(0.0, score) * max(0.0, seg.confidence))
        out.append((token, round(blended, 4), quad))
    return tuple(out)
