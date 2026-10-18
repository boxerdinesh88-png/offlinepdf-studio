"""Page recognition pipeline: render -> preprocess -> OCR -> layout.

Thread-safe: it only touches the PyMuPDF document it is given, so workers
open their own copy of the file instead of sharing the session's view.

Resolution: pages are rendered at ``dpi`` (default 300) for preprocessing,
colour sampling and later inpainting, but recognition runs on a copy scaled
to ``ocr_dpi`` (default 150).  The bundled PP-OCR detector was measured to
split and clip large type at 300 DPI while reading 5 pt text fine at 150;
pages whose text is still tiny at 150 DPI are retried at full resolution.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pymupdf

from .cache import OcrCache
from .engine import OcrError, shared_engine
from .layout import OcrLine, Segment, build_lines, is_vertical
from .preprocess import IDENTITY as IDENTITY_MAP
from .preprocess import Mapping, PreprocessOptions, compose, preprocess, to_homography

__all__ = ["OcrOptions", "recognize_page"]

logger = logging.getLogger(__name__)

_MAX_PIXELS = 40_000_000  # cap huge pages (e.g. A0 at 300 DPI)
_TINY_TEXT_PX = 13.0  # median line height at ocr_dpi below this -> retry at dpi


@dataclass(frozen=True)
class OcrOptions:
    language: str = "en"
    dpi: int = 300
    ocr_dpi: int = 150
    min_confidence: float = 0.6  # lines below are dropped (logos, graphics read as text)
    preprocess: PreprocessOptions = field(default_factory=PreprocessOptions)

    def cache_tag(self) -> str:
        p = self.preprocess
        return (
            f"{self.language}|{self.dpi}|{self.ocr_dpi}|{self.min_confidence}|"
            f"{int(p.deskew)}{int(p.denoise)}{int(p.contrast)}{int(p.binarize)}"
        )


def _render(page: pymupdf.Page, dpi: int) -> tuple[np.ndarray, float]:
    scale = dpi / 72.0
    pixels = page.rect.width * scale * page.rect.height * scale
    if pixels > _MAX_PIXELS:
        scale *= (_MAX_PIXELS / pixels) ** 0.5
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return arr[:, :, :3].copy(), scale


def _fingerprint(page: pymupdf.Page) -> bytes:
    pix = page.get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5), alpha=False, colorspace=pymupdf.csGRAY)
    return f"{page.rect.width:.1f}x{page.rect.height:.1f}|".encode() + bytes(pix.samples)


def _segments(image: np.ndarray, factor: float, language: str) -> list[Segment]:
    """Recognise ``image`` scaled by ``factor``; boxes returned in ``image`` pixels."""
    import cv2

    work = image
    if factor != 1.0:
        work = cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    out: list[Segment] = []
    for word in shared_engine(language).recognize_array(work):
        xs = [p[0] / factor for p in word.box]
        ys = [p[1] / factor for p in word.box]
        out.append(Segment(word.text, word.confidence, min(xs), min(ys), max(xs), max(ys)))
    return out


def recognize_page(
    doc: pymupdf.Document,
    page_index: int,
    options: OcrOptions,
    *,
    cache: OcrCache | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[OcrLine]:
    """Recognise one page; lines are in view-space points.

    Lines overlapping real (extractable) text are dropped, so running this on
    a mixed page only adds what is missing.  Raises :class:`OcrError`.
    """
    try:
        page = doc.load_page(page_index)
    except Exception as exc:
        raise OcrError(f"page {page_index + 1} cannot be read: {exc}") from exc

    key = None
    if cache is not None:
        try:
            key = OcrCache.key(_fingerprint(page), options.cache_tag())
            hit = cache.get(key)
            if hit is not None:
                logger.info("OCR cache hit for page %d", page_index + 1)
                return _without_real_text(page, hit)
        except Exception:  # cache is an optimisation only
            key = None

    try:
        rgb, scale = _render(page, options.dpi)
    except Exception as exc:
        raise OcrError(f"page {page_index + 1} could not be rendered: {exc}") from exc
    if cancelled():
        return []
    try:
        clean, back, skew = preprocess(rgb, options.preprocess)
    except Exception as exc:
        raise OcrError(f"image preprocessing failed: {exc}") from exc
    if skew:
        logger.info("page %d: corrected %.2f° skew", page_index + 1, skew)
    straight = rgb if _is_identity(back) else _straighten(rgb, back, clean.shape[:2])

    factor = min(1.0, options.ocr_dpi / (scale * 72.0))
    segments = _segments(clean, factor, options.language)
    heights = [s.height * factor for s in segments]
    if factor < 1.0 and heights and statistics.median(heights) < _TINY_TEXT_PX:
        if cancelled():
            return []
        segments = _segments(clean, 1.0, options.language)
    if not _reads_well(segments):
        # Maybe scanned sideways/upside down: try the other orientations.
        best = (_score(segments), 0, segments)
        for turn in (90, 180, 270):
            if cancelled():
                return []
            turned = _segments(_rotate(clean, turn), factor, options.language)
            if _score(turned) > best[0]:
                best = (_score(turned), turn, turned)
        _, turn, segments = best
        if turn:
            logger.info("page %d: text reads upright after turning %d°", page_index + 1, turn)
            # Redo clean-up on the upright page: a skew measured on sideways
            # text is meaningless and would inflate every line box.
            h, w = rgb.shape[:2]
            upright = _rotate(rgb, turn)
            try:
                clean, back_upright, skew = preprocess(upright, options.preprocess)
            except Exception as exc:
                raise OcrError(f"image preprocessing failed: {exc}") from exc
            back = compose(back_upright, _unrotate(turn, w, h))
            straight = (
                upright
                if _is_identity(back_upright)
                else _straighten(upright, back_upright, clean.shape[:2])
            )
            segments = _segments(clean, factor, options.language)
    segments = [s for s in segments if s.confidence >= options.min_confidence]
    photo = isinstance(back, np.ndarray)  # a perspective-corrected photograph
    scorer = shared_engine(options.language).recognize_crops
    lines = build_lines(segments, straight, back, scale, photo=photo, score_words=scorer)
    if cancelled():
        return []
    lines += _vertical_lines(clean, straight, back, scale, factor, options, photo=photo)
    if cache is not None and key is not None:
        cache.put(key, lines)
    return _without_real_text(page, lines)


def _vertical_lines(
    clean: np.ndarray,
    straight: np.ndarray,
    back: Mapping,
    scale: float,
    factor: float,
    options: OcrOptions,
    *,
    photo: bool = False,
) -> list[OcrLine]:
    """Text printed vertically (e.g. rotated chart axis titles).

    The detector does not find vertical lines at all, so the page is read
    again turned 90° each way: there the vertical text runs horizontally,
    while the page's normal text stands on end (tall boxes, ignored).  Each
    hit becomes a line rotated by -90/+90 lying exactly on the printed one.
    """
    h, w = clean.shape[:2]
    found: list[OcrLine] = []
    for turn in (270, 90):  # bottom-to-top titles are the most common
        segs = [
            seg
            for seg in _segments(_rotate(clean, turn), factor, options.language)
            if seg.confidence >= options.min_confidence
            and len(seg.text.strip()) >= 2
            and not is_vertical(seg)
        ]
        if not segs:
            continue
        frame = compose(_unrotate(turn, w, h), back)
        found += build_lines(segs, _rotate(straight, turn), frame, scale, photo=photo)
    # The same title may be read in both directions: keep the surer reading.
    kept: list[OcrLine] = []
    for line in sorted(found, key=lambda item: -item.confidence):
        box = _bounds(line)
        if all(_overlap(box, _bounds(other)) < 0.5 for other in kept):
            kept.append(line)
    return kept


def _bounds(line: OcrLine) -> tuple[float, float, float, float]:
    xs = [p[0] for p in line.quad]
    ys = [p[1] for p in line.quad]
    return (min(xs), min(ys), max(xs), max(ys))


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Intersection over the smaller box."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1.0
    return ix * iy / smaller


def _score(segments: list[Segment]) -> float:
    """Recognised characters x confidence, counting only line-shaped boxes.

    Reading a page in the wrong orientation can still yield text from a tall
    box spanning several lines; real lines are wider than they are tall.
    """
    return sum(
        len(s.text.strip()) * s.confidence
        for s in segments
        if (s.x1 - s.x0) >= 1.2 * s.height or len(s.text.strip()) <= 2
    )


def _reads_well(segments: list[Segment]) -> bool:
    chars = sum(len(s.text.strip()) for s in segments)
    if chars == 0:
        return False
    mean = _score(segments) / chars
    return chars >= 40 and mean >= 0.85


def _rotate(image: np.ndarray, turn: int) -> np.ndarray:
    """Turn ``image`` clockwise by 90/180/270 degrees."""
    import cv2

    codes = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }
    return np.ascontiguousarray(cv2.rotate(image, codes[turn]))


def _unrotate(turn: int, w: int, h: int) -> tuple[float, float, float, float, float, float]:
    """Affine from a :func:`_rotate`-d image's pixels back to the original's (w x h)."""
    if turn == 90:
        return (0.0, -1.0, 1.0, 0.0, 0.0, h - 1.0)
    if turn == 180:
        return (-1.0, 0.0, 0.0, -1.0, w - 1.0, h - 1.0)
    return (0.0, 1.0, -1.0, 0.0, w - 1.0, 0.0)


def _is_identity(mapping: Mapping) -> bool:
    return not isinstance(mapping, np.ndarray) and mapping == IDENTITY_MAP


def _straighten(rgb: np.ndarray, back: Mapping, shape: tuple[int, int]) -> np.ndarray:
    """Resample ``rgb`` into the cleaned image's frame (colour sampling)."""
    import cv2

    h, w = shape
    forward = np.linalg.inv(to_homography(back))
    return cv2.warpPerspective(
        rgb, forward, (w, h), flags=cv2.INTER_LINEAR, borderValue=(255, 255, 255)
    )


def _without_real_text(page: pymupdf.Page, lines: list[OcrLine]) -> list[OcrLine]:
    words = [pymupdf.Rect(w[:4]) for w in page.get_text("words") if w[4].strip()]
    if not words:
        return lines
    rotation = page.rotation_matrix
    words = [w * rotation for w in words]  # extraction is unrotated; lines are view space
    kept = []
    for line in lines:
        xs = [p[0] for p in line.quad]
        ys = [p[1] for p in line.quad]
        box = pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))
        covered = sum((box & w).get_area() for w in words if box.intersects(w))
        if covered < 0.3 * max(box.get_area(), 1e-6):
            kept.append(line)
    return kept
