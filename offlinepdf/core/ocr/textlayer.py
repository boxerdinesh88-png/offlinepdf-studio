"""The OCR text layer: invisible real text that turns visible when edited.

Each recognised line is written as one marked-content block::

    /OPDFOcr <</P <json-hex>>> BDC
    q BT 3 Tr <fg> rg /OPDFOcrN <size> Tf <scale> Tz <Tm> Tm <glyphs> Tj ET Q
    EMC

* Render mode 3 keeps the scan's pixels as the visible text while making the
  page searchable and selectable.
* The font is an embedded subset of a bundled font (Liberation Sans, or Noto
  for Devanagari/Arabic/Hebrew/CJK), so the editor's existing text tools edit
  these lines like any other text - no separate editing system.
* ``P`` stores the line's quad, colours, confidence and a fingerprint ``k`` of
  the block's text operators.  After every edit the engine calls
  :func:`reveal_changed`: a block whose operators no longer match ``k`` was
  edited (or moved by a reflow), so the original pixels under its quad are
  covered (flat background colour, or an inpainted patch on textured
  backgrounds) and the text is switched to visible fill mode.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import zlib
from collections.abc import Iterable
from typing import Any

import pikepdf
import pymupdf

from offlinepdf.core.document import PdfSession
from offlinepdf.core.history import Command
from offlinepdf.core.image_ops import mat_mul, view_to_pdf_matrix

from .detect import OCR_MARK
from .layout import OcrLine

__all__ = [
    "OcrBlockInfo",
    "OcrLayerCommand",
    "apply_ocr_layer",
    "make_page_editable",
    "page_ocr_blocks",
    "rerecognise_page",
    "reveal_changed",
    "revert_page",
    "strip_unedited",
]

logger = logging.getLogger(__name__)

_MARK = "/OPDFOcr"
_FONT_PREFIX = "OPDFOcr"
_PATCH_PREFIX = "OPDFOcrPatch"
_PATCH_SCALE = 3.0
_COVER_PAD = 0.12  # x font size, around the recognised quad

_FONT_FILES = {
    "LiberationSans": ("LiberationSans/LiberationSans-Regular.ttf",
                       "LiberationSans/LiberationSans-Bold.ttf"),
    "LiberationSerif": ("LiberationSerif/LiberationSerif-Regular.ttf",
                        "LiberationSerif/LiberationSerif-Bold.ttf"),
    "LiberationMono": ("LiberationMono/LiberationMono-Regular.ttf",
                       "LiberationMono/LiberationMono-Bold.ttf"),
    "Carlito": ("Carlito/Carlito-Regular.ttf", "Carlito/Carlito-Bold.ttf"),
    "NotoSansDevanagari": ("NotoSansDevanagari/NotoSansDevanagari-Regular.ttf",
                           "NotoSansDevanagari/NotoSansDevanagari-Bold.ttf"),
    "NotoSansArabic": ("NotoSansArabic/NotoSansArabic-Regular.ttf",
                       "NotoSansArabic/NotoSansArabic-Bold.ttf"),
    "NotoSansHebrew": ("NotoSansHebrew/NotoSansHebrew-Regular.ttf",
                       "NotoSansHebrew/NotoSansHebrew-Bold.ttf"),
    "NotoSansSC": ("NotoSansSC/NotoSansSC-Regular.ttf", "NotoSansSC/NotoSansSC-Bold.ttf"),
}


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #
def _font_path(family: str, bold: bool, italic: bool = False) -> str:
    """Bundled face file; italic variants follow the ``-Italic``/``-BoldItalic`` naming."""
    from offlinepdf.config import resource_path

    regular, heavy = _FONT_FILES.get(family, _FONT_FILES["LiberationSans"])
    chosen = heavy if bold else regular
    candidates = []
    if italic:
        stem = chosen[: -len(".ttf")]
        candidates.append(stem.replace("-Regular", "") + ("Italic" if bold else "-Italic") + ".ttf"
                          if bold else stem.replace("-Regular", "-Italic") + ".ttf")
    candidates += [chosen, regular]
    for rel in candidates:
        path = resource_path("assets", "fonts", *rel.split("/"))
        if path.exists():
            return str(path)
    return str(resource_path("assets", "fonts", *regular.split("/")))


def _num(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _rgb(color: Iterable[float]) -> str:
    return " ".join(_num(max(0.0, min(1.0, float(c)))) for c in color)


def _norm(operand: Any) -> Any:
    if isinstance(operand, (int, float)):
        return round(float(operand), 3)
    if isinstance(operand, pikepdf.String):
        return bytes(operand)
    if isinstance(operand, pikepdf.Array):
        return tuple(_norm(item) for item in operand)
    if isinstance(operand, pikepdf.Object):
        try:
            return round(float(operand), 3)
        except (TypeError, ValueError):
            return str(operand)
    return str(operand)


def _fingerprint(instructions: Iterable[Any]) -> str:
    """Stable hash of the text operators (render mode excluded)."""
    digest = hashlib.sha1()
    for inst in instructions:
        op = str(getattr(inst, "operator", ""))
        if op == "Tr":
            continue
        operands = tuple(_norm(o) for o in getattr(inst, "operands", []))
        digest.update(repr((op, operands)).encode())
    return digest.hexdigest()[:16]


def _text_ops(instructions: list[Any]) -> list[Any]:
    """The BT..ET part of a block (inclusive)."""
    ops = [str(getattr(i, "operator", "")) for i in instructions]
    try:
        start, end = ops.index("BT"), len(ops) - 1 - ops[::-1].index("ET")
    except ValueError:
        return []
    return instructions[start : end + 1]


_SCRATCH: pikepdf.Pdf | None = None


def _parse(ops: str) -> list[Any]:
    """Parse a content snippet (kept alive by a module-level scratch PDF)."""
    global _SCRATCH
    if _SCRATCH is None:
        _SCRATCH = pikepdf.new()
    return list(pikepdf.parse_content_stream(pikepdf.Stream(_SCRATCH, ops.encode())))


def _quad_path(quad: Any, pad: float) -> str:
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    cx, cy = sum(xs) / 4.0, sum(ys) / 4.0
    pts = []
    for x, y in quad:
        dx, dy = x - cx, y - cy
        length = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        pts.append((x + dx / length * pad, y + dy / length * pad))
    first, *rest = pts
    return (
        f"{_num(first[0])} {_num(first[1])} m "
        + " ".join(f"{_num(x)} {_num(y)} l" for x, y in rest)
        + " h"
    )


def _own_resources(page: pikepdf.Page) -> pikepdf.Dictionary:
    from offlinepdf.core.image_ops import _own_resources as own

    return own(page)


def _free_name(container: Any, prefix: str) -> str:
    index = 0
    while f"/{prefix}{index}" in container:
        index += 1
    return f"{prefix}{index}"


# ---------------------------------------------------------------------- #
# writing the layer
# ---------------------------------------------------------------------- #
class _PageFont:
    """One embedded subset per (family, bold) on a page."""

    def __init__(
        self, pdf: pikepdf.Pdf, family: str, bold: bool, text: str, italic: bool = False
    ) -> None:
        from fontTools.ttLib import TTFont

        from offlinepdf.core.edit_engine import _embed_program, _font_object, _ToUnicodePlan

        path = _font_path(family, bold, italic)
        font = TTFont(path, lazy=True)
        try:
            cmap = font.getBestCmap() or {}
        finally:
            font.close()
        chars = "".join(dict.fromkeys(c for c in text if ord(c) in cmap and c.isprintable()))
        embedded = _embed_program(path, 0, chars or " ")
        if embedded is None:
            raise RuntimeError(f"cannot embed {family}")
        self.embedded = embedded
        pairs = [(code, char) for char, code in embedded.char_bytes.items()]
        self.obj = _font_object(pdf, embedded, _ToUnicodePlan(mode="fresh", pairs=pairs))

    def encode(self, text: str) -> tuple[bytes, float]:
        """Glyph codes for ``text`` (unsupported characters dropped) and width/1000."""
        codes = bytearray()
        width = 0.0
        for char in text:
            code = self.embedded.char_bytes.get(char)
            if code is None:
                continue
            codes += code
            width += self.embedded.widths[int.from_bytes(code, "big")]
        return bytes(codes), width


def _line_block(line: OcrLine, font_name: str, font: _PageFont, to_pdf: Any) -> str | None:
    codes, width_units = font.encode(line.text)
    if not codes:
        return None
    natural = width_units * line.size / 1000.0
    hscale = 100.0 if natural <= 0 else max(50.0, min(200.0, 100.0 * line.width / natural))
    rad = math.radians(line.angle)
    cos, sin = math.cos(rad), math.sin(rad)
    # Text space is y-up; view space is y-down and rotated clockwise by ``angle``.
    tm = mat_mul((cos, sin, sin, -cos, line.origin[0], line.origin[1]), to_pdf)
    inner = "\n".join(
        [
            "BT",
            "3 Tr",
            f"{_rgb(line.fg)} rg",
            f"/{font_name} {_num(line.size)} Tf",
            f"{_num(hscale)} Tz",
            " ".join(_num(v) for v in tm) + " Tm",
            f"<{codes.hex()}> Tj",
            "ET",
        ]
    )
    key = _fingerprint(_parse(inner))
    params = {
        "t": line.text,
        "c": round(line.confidence, 4),
        "q": [[round(x, 3), round(y, 3)] for x, y in line.quad],
        "cq": [[round(x, 3), round(y, 3)] for x, y in line.cover] if line.cover else None,
        "w": [
            [word, round(conf, 4), [[round(x, 2), round(y, 2)] for x, y in quad]]
            for word, conf, quad in line.words
        ],
        "s": round(line.size, 3),
        "fg": [round(c, 4) for c in line.fg],
        "bg": [round(c, 4) for c in line.bg],
        "u": int(line.bg_uniform),
        "e": 0,
        "k": key,
    }
    props = json.dumps(params, separators=(",", ":"), ensure_ascii=False).encode().hex()
    return f"{_MARK} <</P <{props}>>> BDC\nq\n{inner}\nQ\nEMC\n"


def apply_ocr_layer(session: PdfSession, page_index: int, lines: list[OcrLine]) -> int:
    """Write ``lines`` into page ``page_index`` of the session's document.

    Mutates ``session.pdf`` and refreshes the view, without marking the
    document dirty (nothing visible changed).  Returns the lines written.
    """
    if not lines:
        return 0
    pdf = session.pdf
    page = pdf.pages[page_index]
    to_pdf = view_to_pdf_matrix(session.mupdf.load_page(page_index))
    resources = _own_resources(page)
    if "/Font" not in resources:
        resources.Font = pikepdf.Dictionary()
    groups: dict[tuple[str, bool, bool], list[OcrLine]] = {}
    for line in lines:
        groups.setdefault((line.font, line.bold, line.italic), []).append(line)
    blocks: list[str] = []
    for (family, bold, italic), group in groups.items():
        try:
            font = _PageFont(pdf, family, bold, "".join(line.text for line in group), italic)
        except Exception:
            logger.exception("OCR layer: font %s unavailable; %d lines skipped", family, len(group))
            continue
        name = _free_name(resources.Font, _FONT_PREFIX)
        resources.Font[f"/{name}"] = pdf.make_indirect(font.obj)
        for line in group:
            block = _line_block(line, name, font, to_pdf)
            if block is not None:
                blocks.append(block)
    if not blocks:
        return 0
    # Scanners often leave a page-sized ``cm`` (or a y-flip) in effect after
    # drawing the image, without q/Q.  Isolate the original content so the
    # layer is placed in default user space, where its matrices are computed.
    page.contents_add(b"q\n", prepend=True)
    page.contents_add(b"\nQ\n" + "".join(blocks).encode())
    session.refresh_view()
    return len(blocks)


class OcrLayerCommand(Command):
    """Undo/redo of "make page editable" (whole-document snapshots)."""

    def __init__(self, description: str, before: bytes, after: bytes) -> None:
        self.description = description
        self._before = before
        self._after = after

    def redo(self, session: PdfSession) -> None:
        session.replace_bytes(zlib.decompress(self._after))

    def undo(self, session: PdfSession) -> None:
        session.replace_bytes(zlib.decompress(self._before))


def rerecognise_page(session: PdfSession, page_index: int, lines: list[OcrLine]) -> int:
    """Replace a page's unedited OCR lines with ``lines`` (one undo step).

    Lines the user already edited are real visible text by now, so the new
    recognition drops anything overlapping them (see the pipeline).
    """
    before = zlib.compress(session.to_bytes(), 6)
    strip_unedited(session.pdf, [page_index])
    session.refresh_view()
    kept = _drop_over_visible_text(session, page_index, lines)
    written = apply_ocr_layer(session, page_index, kept)
    after = zlib.compress(session.to_bytes(), 6)
    session.history.record(
        OcrLayerCommand(f"re-recognise page {page_index + 1}", before, after)
    )
    return written


def _drop_over_visible_text(
    session: PdfSession, page_index: int, lines: list[OcrLine]
) -> list[OcrLine]:
    page = session.mupdf.load_page(page_index)
    words = [pymupdf.Rect(w[:4]) * page.rotation_matrix for w in page.get_text("words")]
    kept = []
    for line in lines:
        xs = [p[0] for p in line.quad]
        ys = [p[1] for p in line.quad]
        box = pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))
        covered = sum((box & w).get_area() for w in words if box.intersects(w))
        if covered < 0.3 * max(box.get_area(), 1e-6):
            kept.append(line)
    return kept


def make_page_editable(session: PdfSession, page_index: int, lines: list[OcrLine]) -> int:
    """:func:`apply_ocr_layer` recorded as one undoable step."""
    before = zlib.compress(session.to_bytes(), 6)
    written = apply_ocr_layer(session, page_index, lines)
    if written:
        after = zlib.compress(session.to_bytes(), 6)
        session.history.record(
            OcrLayerCommand(f"make page {page_index + 1} editable", before, after)
        )
    return written


# ---------------------------------------------------------------------- #
# reading / revealing
# ---------------------------------------------------------------------- #
class OcrBlockInfo:
    """Parameters of one OCR line block as stored in the page."""

    def __init__(self, params: dict[str, Any], start: int, end: int) -> None:
        self.params = params
        self.start = start  # instruction index of BDC
        self.end = end  # instruction index of EMC

    @property
    def text(self) -> str:
        return str(self.params.get("t", ""))

    @property
    def confidence(self) -> float:
        return float(self.params.get("c", 1.0))

    @property
    def quad(self) -> list[tuple[float, float]]:
        return [(float(x), float(y)) for x, y in self.params.get("q", [])]

    @property
    def cover_quad(self) -> list[tuple[float, float]]:
        """Area to cover when the line is edited (tight ink box when known)."""
        tight = self.params.get("cq")
        if tight:
            return [(float(x), float(y)) for x, y in tight]
        return self.quad

    @property
    def words(self) -> list[tuple[str, float, list[tuple[float, float]]]]:
        """Per-word (text, confidence, quad) when the line was reviewed word by word."""
        return [
            (str(word), float(conf), [(float(x), float(y)) for x, y in quad])
            for word, conf, quad in self.params.get("w") or ()
        ]

    @property
    def edited(self) -> bool:
        return bool(self.params.get("e", 0))


def _blocks(instructions: list[Any]) -> list[OcrBlockInfo]:
    found: list[OcrBlockInfo] = []
    depth = 0
    start = -1
    params: dict[str, Any] = {}
    for index, inst in enumerate(instructions):
        op = str(getattr(inst, "operator", ""))
        if op in ("BMC", "BDC"):
            operands = list(getattr(inst, "operands", []))
            if depth == 0 and op == "BDC" and operands and str(operands[0]) == _MARK:
                try:
                    params = json.loads(bytes(operands[1].P).decode("utf-8"))
                    start = index
                except Exception:
                    start = -1
            depth += 1
        elif op == "EMC" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                found.append(OcrBlockInfo(params, start, index))
                start = -1
    return found


def _page_has_layer(page: pikepdf.Page) -> bool:
    contents = page.obj.get("/Contents")
    if contents is None:
        return False
    streams = contents if isinstance(contents, pikepdf.Array) else [contents]
    try:
        return any(OCR_MARK in s.read_bytes() for s in streams)
    except Exception:
        return False


def page_ocr_blocks(pdf: pikepdf.Pdf, page_index: int) -> list[OcrBlockInfo]:
    """OCR line blocks on a page (empty for pages without a layer)."""
    page = pdf.pages[page_index]
    if not _page_has_layer(page):
        return []
    try:
        return _blocks(list(pikepdf.parse_content_stream(page)))
    except pikepdf.PdfError:
        return []


def _patch_image(
    view_page: pymupdf.Page, quad: list[tuple[float, float]], pad: float, size: float
) -> Any:
    """Inpainted replacement for the line's *text pixels only*.

    Returns ``(rgb, alpha, rect)``: ``alpha`` is a soft mask of the glyphs
    (ruling lines removed), so axes, ticks, borders, stamps and photos
    under or around the text are left exactly as scanned.
    """
    import cv2
    import numpy as np

    from .layout import strip_rules

    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    rect = pymupdf.Rect(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
    rect &= view_page.rect
    if rect.is_empty:
        return None
    pix = view_page.get_pixmap(
        matrix=pymupdf.Matrix(_PATCH_SCALE, _PATCH_SCALE), clip=rect, alpha=False
    )
    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    rgb = np.ascontiguousarray(arr)
    gray8 = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = gray8.astype(np.int16)
    _t, dark = cv2.threshold(gray8, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    light_text = dark.mean() > 127  # light text on a dark background
    # Compare every pixel with its *local* paper level (a morphological
    # closing about a glyph wide), so shading and vignetting in photos never
    # count as ink; faint anti-aliased glyph edges still do.
    k = max(5, round(size * _PATCH_SCALE * 1.2)) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    op = cv2.MORPH_OPEN if light_text else cv2.MORPH_CLOSE
    local = cv2.morphologyEx(gray8, op, kernel).astype(np.int16)
    uneven = float(np.ptp(local)) >= 20  # photo-like shading: blurrier edges
    margin = 16 if uneven else 24
    ink = (gray > local + margin) if light_text else (gray < local - margin)
    # Coloured (sub-pixel) anti-aliasing fringes are about as bright as the
    # paper but clearly tinted: compare colour too, where the paper is even.
    if not light_text and not uneven:
        bright = rgb[gray >= float(np.percentile(gray, 90)) - 2]
        paper_rgb = np.median(bright, axis=0) if bright.size else np.array([255, 255, 255])
        tint = np.abs(rgb.astype(np.int16) - paper_rgb.astype(np.int16)).max(axis=2)
        ink |= tint > 16
    ink = strip_rules(ink, line_height=size * _PATCH_SCALE * 1.06)
    if not ink.any():
        return None
    grow = max(3, round(size * _PATCH_SCALE * (0.09 if uneven else 0.05))) | 1
    mask = cv2.dilate(ink.astype(np.uint8) * 255, np.ones((grow, grow), np.uint8), iterations=1)
    # Fill = the local average of the surrounding paper (normalised
    # convolution over non-text pixels): it follows shading and keeps the
    # paper's mean tone, without inpainting smears or too-bright blocks.
    paper_w = (mask == 0).astype(np.float32)
    sigma = max(2.0, k / 2.0)
    weight = cv2.GaussianBlur(paper_w, (0, 0), sigma)
    total = cv2.GaussianBlur(rgb.astype(np.float32) * paper_w[..., None], (0, 0), sigma)
    paper_fill = total / np.maximum(weight[..., None], 1e-3)
    filled = np.where(mask[..., None] > 0, paper_fill, rgb)
    filled = np.clip(filled, 0, 255).astype(np.uint8)
    # Full coverage over every old glyph pixel; feather only beyond them.
    alpha = cv2.GaussianBlur(cv2.dilate(mask, np.ones((9, 9), np.uint8)), (5, 5), 0)
    return filled, alpha, rect


def reveal_changed(session: PdfSession, page_index: int) -> int:
    """Cover + show every OCR line whose text operators changed.

    Called by the edit engine after it rewrote a page but *before* the view
    is refreshed, so ``session.mupdf`` still shows the original pixels used
    for inpainting.  Returns the number of lines revealed.
    """
    pdf = session.pdf
    page = pdf.pages[page_index]
    if not _page_has_layer(page):
        return 0
    try:
        instructions = list(pikepdf.parse_content_stream(page))
    except pikepdf.PdfError:
        return 0
    blocks = [b for b in _blocks(instructions) if not b.edited]
    changed = [
        b for b in blocks
        if _fingerprint(_text_ops(instructions[b.start + 1 : b.end])) != b.params.get("k")
    ]
    if not changed:
        return 0
    view_page = session.mupdf.load_page(page_index)
    to_pdf = view_to_pdf_matrix(view_page)
    cm = " ".join(_num(v) for v in to_pdf) + " cm"
    resources = _own_resources(page)
    replacements: dict[int, list[Any]] = {}
    for block in changed:
        params = dict(block.params)
        params["e"] = 1
        tight = bool(params.get("cq"))
        # A little margin even around the tight ink box: blurred (photo) glyph
        # edges and descenders reach slightly beyond the measured band.
        pad = (0.1 if tight else _COVER_PAD) * float(params.get("s", 10.0))
        quad = block.cover_quad
        cover = ""
        # Replace only the glyph pixels (soft-masked inpainted patch), clipped to
        # the possibly tilted cover quad, so nothing else under the text changes.
        size = float(params.get("s", 10.0))
        # Render with a margin so ruling lines are seen whole (a glyph stroke
        # never runs past its line) and inpainting fills from clean paper;
        # painting stays clipped to the tight quad below.
        patch = _patch_image(view_page, quad, max(pad, 0.6 * size), size) if quad else None
        if patch is not None:
            filled, alpha, rect = patch
            if "/XObject" not in resources:
                resources.XObject = pikepdf.Dictionary()
            name = _free_name(resources.XObject, _PATCH_PREFIX)
            resources.XObject[f"/{name}"] = _patch_xobject(pdf, filled, alpha)
            place = " ".join(
                _num(v) for v in (rect.width, 0.0, 0.0, -rect.height, rect.x0, rect.y1)
            )
            cover = f"q {cm} {_quad_path(quad, pad)} W n {place} cm /{name} Do Q"
        head = pikepdf.ContentStreamInstruction(
            [pikepdf.Name(_MARK), pikepdf.Dictionary(P=pikepdf.String(
                json.dumps(params, separators=(",", ":"), ensure_ascii=False).encode()
            ))],
            pikepdf.Operator("BDC"),
        )
        cover_ops = _parse(cover) if cover else []
        body: list[Any] = []
        for inst in instructions[block.start + 1 : block.end]:
            if str(getattr(inst, "operator", "")) == "Tr":
                inst = pikepdf.ContentStreamInstruction([0], pikepdf.Operator("Tr"))
            body.append(inst)
        # Cover first (inside the block's q, before the text).
        if body and str(getattr(body[0], "operator", "")) == "q":
            body = [body[0], *cover_ops, *body[1:]]
        else:
            body = [*cover_ops, *body]
        replacements[block.start] = [head, *body, instructions[block.end]]
    rebuilt: list[Any] = []
    skip_until = -1
    for index, inst in enumerate(instructions):
        if index <= skip_until:
            continue
        if index in replacements:
            rebuilt.extend(replacements[index])
            skip_until = next(b.end for b in changed if b.start == index)
            continue
        rebuilt.append(inst)
    page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(rebuilt))
    return len(changed)


def _patch_xobject(pdf: pikepdf.Pdf, rgb: Any, alpha: Any) -> pikepdf.Object:
    """RGB image XObject with a soft mask (only masked pixels are painted)."""
    height, width = alpha.shape[:2]
    smask = pdf.make_stream(
        zlib.compress(alpha.tobytes(), 9),
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Image,
        Width=width,
        Height=height,
        ColorSpace=pikepdf.Name.DeviceGray,
        BitsPerComponent=8,
        Filter=pikepdf.Name.FlateDecode,
    )
    image = pdf.make_stream(
        zlib.compress(rgb.tobytes(), 9),
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Image,
        Width=width,
        Height=height,
        ColorSpace=pikepdf.Name.DeviceRGB,
        BitsPerComponent=8,
        Filter=pikepdf.Name.FlateDecode,
        SMask=smask,
    )
    return pdf.make_indirect(image)


def revert_page(session: PdfSession, page_index: int) -> int:
    """Remove the whole OCR layer of a page, edits included (one undo step).

    The page shows the original scan again: recognised text, covers and the
    fonts/patches they used are dropped.  Returns the lines removed.
    """
    pdf = session.pdf
    page = pdf.pages[page_index]
    if not _page_has_layer(page):
        return 0
    before = zlib.compress(session.to_bytes(), 6)
    instructions = list(pikepdf.parse_content_stream(page))
    blocks = _blocks(instructions)
    drop: set[int] = set()
    for block in blocks:
        drop.update(range(block.start, block.end + 1))
    kept = [inst for index, inst in enumerate(instructions) if index not in drop]
    page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(kept))
    resources = page.obj.get("/Resources")
    if resources is not None:
        for group, prefix in (("/Font", _FONT_PREFIX), ("/XObject", _PATCH_PREFIX)):
            container = resources.get(group)
            if container is None:
                continue
            for name in [n for n in container.keys() if str(n).startswith(f"/{prefix}")]:  # noqa: SIM118
                del container[name]
    session.refresh_view()
    session.mark_dirty()
    after = zlib.compress(session.to_bytes(), 6)
    session.history.record(
        OcrLayerCommand(f"revert page {page_index + 1} to original scan", before, after)
    )
    return len(blocks)


def strip_unedited(pdf: pikepdf.Pdf, pages: Iterable[int] | None = None) -> int:
    """Remove OCR lines nobody edited (export without a searchable layer).

    ``pages`` limits it to some page indices (re-recognising a page).
    """
    removed = 0
    indices = range(len(pdf.pages)) if pages is None else pages
    for index in indices:
        page = pdf.pages[index]
        if not _page_has_layer(page):
            continue
        instructions = list(pikepdf.parse_content_stream(page))
        drop: set[int] = set()
        for block in _blocks(instructions):
            if not block.edited:
                drop.update(range(block.start, block.end + 1))
                removed += 1
        if drop:
            kept = [inst for i, inst in enumerate(instructions) if i not in drop]
            page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(kept))
    return removed
