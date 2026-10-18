"""Image clean-up before recognition: grayscale, deskew, denoise, contrast.

Every step is optional (:class:`PreprocessOptions`).  Deskewing rotates the
image, so :func:`preprocess` also returns the affine matrix that maps points
of the cleaned image back onto the original rendering - recognised boxes are
pushed through it so text lands where the user sees it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = [
    "Affine",
    "Mapping",
    "PreprocessOptions",
    "compose",
    "estimate_skew",
    "find_page_quad",
    "map_point",
    "preprocess",
    "to_homography",
]

Affine = tuple[float, float, float, float, float, float]  # x' = a x + c y + e, y' = b x + d y + f
#: affine 6-tuple, or a 3x3 homography (photographed pages: perspective)
Mapping = Affine | np.ndarray
IDENTITY: Affine = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_MAX_SKEW = 15.0
_MIN_SKEW = 0.25


@dataclass(frozen=True)
class PreprocessOptions:
    deskew: bool = True
    denoise: bool = True
    contrast: bool = True
    binarize: bool = False  # PP-OCR reads clean grayscale better than hard B/W
    #: photographed pages: straighten the page outline (perspective) and
    #: even out uneven lighting before reading
    photo: bool = True


def _cv2() -> Any:
    import cv2

    return cv2


def map_point(m: Mapping, x: float, y: float) -> tuple[float, float]:
    if isinstance(m, np.ndarray):
        px, py, pw = m @ np.array([x, y, 1.0])
        return (float(px / pw), float(py / pw))
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def to_homography(m: Mapping) -> np.ndarray:
    if isinstance(m, np.ndarray):
        return m.astype(np.float64)
    a, b, c, d, e, f = m
    return np.array([[a, c, e], [b, d, f], [0.0, 0.0, 1.0]], dtype=np.float64)


def compose(first: Mapping, then: Mapping) -> Mapping:
    """Mapping applying ``first`` and then ``then`` (affine stays affine)."""
    if isinstance(first, np.ndarray) or isinstance(then, np.ndarray):
        return to_homography(then) @ to_homography(first)
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = then
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def find_page_quad(gray: np.ndarray) -> np.ndarray | None:
    """Corners (tl, tr, br, bl) of a photographed sheet, or None.

    Only a clearly bounded sheet counts: a bright quadrilateral covering at
    least a quarter of the frame whose corners are not simply the image
    corners (that is a scan, which needs no correction).
    """
    cv2 = _cv2()
    h, w = gray.shape[:2]
    scale = min(1.0, 800.0 / max(h, w))
    small = cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))))
    small = cv2.GaussianBlur(small, (5, 5), 0)
    _t, bright = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    contours, _hier = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    sheet = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(sheet)
    sh, sw = small.shape[:2]
    if area < 0.25 * sh * sw:
        return None
    approx = cv2.approxPolyDP(sheet, 0.02 * cv2.arcLength(sheet, True), True)
    if len(approx) != 4 or not cv2.isContourConvex(approx):
        return None
    pts = approx.reshape(4, 2).astype(np.float64) / scale
    order = np.zeros((4, 2))
    sums, diffs = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    order[0], order[2] = pts[np.argmin(sums)], pts[np.argmax(sums)]
    order[1], order[3] = pts[np.argmin(diffs)], pts[np.argmax(diffs)]
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64)
    if np.all(np.linalg.norm(order - corners, axis=1) < 0.03 * max(w, h)):
        return None  # the whole frame: a scan, not a photo
    return order


def _flatten_lighting(gray: np.ndarray) -> np.ndarray:
    """Divide out a smooth background estimate (shadows, vignetting, gradients)."""
    cv2 = _cv2()
    size = max(15, (max(gray.shape) // 25) | 1)
    background = cv2.morphologyEx(
        gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (size, size))
    )
    background = cv2.GaussianBlur(background, (0, 0), size / 2.0)
    flat = gray.astype(np.float32) / np.maximum(background.astype(np.float32), 1.0) * 255.0
    return np.clip(flat, 0, 255).astype(np.uint8)


def _uneven_lighting(gray: np.ndarray) -> bool:
    cv2 = _cv2()
    small = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)
    paper = cv2.morphologyEx(small, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    return float(np.percentile(paper, 95) - np.percentile(paper, 5)) > 40.0


def estimate_skew(gray: np.ndarray) -> float:
    """Dominant text-line angle in degrees (clockwise positive on screen).

    Projection-profile search: rotate a small binarised copy and keep the
    angle whose row sums are the most "peaky" (lines of text vs. gaps).
    """
    cv2 = _cv2()
    h, w = gray.shape[:2]
    scale = min(1.0, 900.0 / max(h, w))
    small = cv2.resize(gray, (max(1, int(w * scale)), max(1, int(h * scale))))
    _t, ink = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    if ink.mean() > 127:  # light text on a dark background: ink is the minority
        ink = 255 - ink
    if ink.mean() < 0.5:  # (almost) nothing printed
        return 0.0
    center = (ink.shape[1] / 2.0, ink.shape[0] / 2.0)

    def score(angle: float) -> float:
        m = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(ink, m, (ink.shape[1], ink.shape[0]), flags=cv2.INTER_NEAREST)
        rows = rotated.sum(axis=1, dtype=np.float64)
        return float(np.var(rows))

    best, best_score = 0.0, score(0.0)
    for angle in np.arange(-_MAX_SKEW, _MAX_SKEW + 0.01, 1.0):
        s = score(float(angle))
        if s > best_score:
            best, best_score = float(angle), s
    low, high = max(-_MAX_SKEW, best - 1.0), min(_MAX_SKEW, best + 1.0)
    for angle in np.arange(low, high + 0.01, 0.1):
        s = score(float(angle))
        if s > best_score:
            best, best_score = float(angle), s
    # cv2 rotates counter-clockwise: undoing a clockwise skew of ``s`` needs ``s``.
    return 0.0 if abs(best) < _MIN_SKEW else round(best, 2)


def preprocess(
    rgb: np.ndarray, options: PreprocessOptions
) -> tuple[np.ndarray, Mapping, float]:
    """Clean ``rgb`` (H x W x 3, uint8).

    Returns ``(image, back, skew)``: the RGB image handed to the recogniser,
    the affine mapping its pixel coordinates back to ``rgb``'s, and the
    detected skew in degrees.
    """
    cv2 = _cv2()
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    back: Mapping = IDENTITY
    skew = 0.0
    if options.photo:
        quad = find_page_quad(gray)
        if quad is not None:
            tl, tr, br, bl = quad
            width = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
            height = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
            target = np.array(
                [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
                dtype=np.float32,
            )
            warp = cv2.getPerspectiveTransform(quad.astype(np.float32), target)
            gray = cv2.warpPerspective(gray, warp, (width, height), flags=cv2.INTER_CUBIC)
            back = np.linalg.inv(warp)  # straightened sheet -> photo pixels
        if quad is not None or _uneven_lighting(gray):
            gray = _flatten_lighting(gray)
    if options.deskew:
        skew = estimate_skew(gray)
        if skew:
            h, w = gray.shape[:2]
            center = (w / 2.0, h / 2.0)
            # Rotating counter-clockwise by the skew straightens the text.
            fwd = cv2.getRotationMatrix2D(center, skew, 1.0)
            gray = cv2.warpAffine(
                gray, fwd, (w, h), flags=cv2.INTER_CUBIC, borderValue=255
            )
            inv = cv2.invertAffineTransform(fwd)
            rotation: Affine = (
                float(inv[0, 0]), float(inv[1, 0]), float(inv[0, 1]),
                float(inv[1, 1]), float(inv[0, 2]), float(inv[1, 2]),
            )
            back = compose(rotation, back)
    if options.denoise:
        gray = cv2.medianBlur(gray, 3)
    if options.contrast:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
    if options.binarize:
        gray = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
        )
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB), back, skew


def rotation_of(back: Mapping) -> float:
    """Rotation (degrees, clockwise on screen) at the origin of ``back``."""
    x0, y0 = map_point(back, 0.0, 0.0)
    x1, y1 = map_point(back, 100.0, 0.0)
    return math.degrees(math.atan2(y1 - y0, x1 - x0))
