"""Inserted images: model, undo commands, PDF baking and lifting.

Design notes (see docs/DECISIONS.md, "Images"):
- Inserted images live in an :class:`ImageLayer` next to the pikepdf
  document instead of inside it.  Moving, resizing, cropping and rotating
  only change small frozen :class:`ImageItem` records, so every action is a
  cheap undoable command and cropping stays non-destructive.
- Geometry is stored in *view space*: PyMuPDF ``page.rect`` points, origin at
  the displayed top-left, y down, /Rotate already applied.  That is exactly
  what the viewer paints, so the screen and the export share one matrix
  (:func:`item_matrix`).
- On save the layer is *baked* into a copy of the document
  (:func:`bake_images`).  Each image becomes one marked-content block
  ``/OPDFImage <</P (json)>> BDC ... EMC`` that carries its edit parameters;
  reopening the file *lifts* those blocks back into the layer
  (:func:`lift_images`) so images stay editable across save/reopen.
- Crop is a clip path over the full (downscaled) image, never a pixel crop,
  so a crop can be re-adjusted after reopening.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import logging
import math
import uuid
import zlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import pikepdf
import pymupdf
from PIL import Image, ImageOps

from offlinepdf.core.history import Command

__all__ = [
    "DEFAULT_JPEG_QUALITY",
    "DEFAULT_MAX_PX",
    "SUPPORTED_EXTENSIONS",
    "ExportOptions",
    "ImageError",
    "ImageItem",
    "ImageLayer",
    "ImageLayerCommand",
    "ImageSource",
    "bake_images",
    "default_placement",
    "display_crop",
    "item_matrix",
    "lift_images",
    "load_image_source",
    "mat_apply",
    "mat_invert",
    "mat_mul",
    "rotate_vec",
    "source_crop",
    "view_to_pdf_matrix",
]

logger = logging.getLogger(__name__)

Matrix = tuple[float, float, float, float, float, float]
Crop = tuple[float, float, float, float]  # left, top, right, bottom fractions
Color = tuple[int, int, int]

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
DEFAULT_MAX_PX = 3000
DEFAULT_JPEG_QUALITY = 90
MIN_SIZE_PT = 4.0
_MAX_DECODE_PIXELS = 120_000_000  # refuse decompression bombs (~11k x 11k)
_SCREEN_DPI = 96.0
_MARK_IMAGE = "/OPDFImage"
_MARK_WRAP = "/OPDFWrap"
_RES_PREFIX_IMAGE = "OPDFIm"
_RES_PREFIX_GS = "OPDFGs"
_PARAMS_VERSION = 1
_KAPPA = 0.5522847498


class ImageError(Exception):
    """Friendly image problem (message is user-safe)."""


# ---------------------------------------------------------------------- #
# sources
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class ImageSource:
    """Immutable decoded-on-demand image data shared by items.

    ``data`` is JPEG bytes (``fmt == "jpeg"``) or PNG bytes (everything
    else; WebP and clipboard images are converted to PNG on load).
    """

    key: str
    data: bytes = field(repr=False)
    fmt: str  # "jpeg" | "png"
    width: int
    height: int
    has_alpha: bool


def _has_alpha(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA", "PA"):
        low = img.getchannel("A").getextrema()[0]
        return isinstance(low, (int, float)) and low < 255
    return img.mode == "P" and "transparency" in img.info


def load_image_source(data: bytes) -> ImageSource:
    """Validate PNG/JPEG/WebP bytes and normalise them into a source.

    JPEGs are kept byte-for-byte (so export can pass them through without
    recompression) unless EXIF orientation needs baking in.  WebP and other
    formats are re-encoded as PNG, which every PDF writer can embed.
    """
    Image.MAX_IMAGE_PIXELS = _MAX_DECODE_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        kind = (img.format or "").upper()
        if kind not in ("PNG", "JPEG", "MPO", "WEBP"):
            raise ImageError("Only PNG, JPG/JPEG and WebP images can be inserted.")
        img.load()
    except ImageError:
        raise
    except Image.DecompressionBombError as exc:
        raise ImageError("The image is too large to insert.") from exc
    except Exception as exc:
        raise ImageError("The file is not a readable image.") from exc

    key = hashlib.sha256(data).hexdigest()[:24]
    orientation = img.getexif().get(0x0112, 1)
    if kind in ("JPEG", "MPO"):
        if orientation in (1, None) and img.mode in ("RGB", "L", "CMYK"):
            return ImageSource(key, data, "jpeg", img.width, img.height, False)
        upright = ImageOps.exif_transpose(img).convert("RGB")
        buf = io.BytesIO()
        upright.save(buf, "JPEG", quality=95, subsampling=0)
        return ImageSource(key, buf.getvalue(), "jpeg", upright.width, upright.height, False)

    upright = ImageOps.exif_transpose(img) if orientation not in (1, None) else img
    if upright.mode not in ("RGB", "RGBA", "L", "LA"):
        upright = upright.convert("RGBA" if _has_alpha(upright) else "RGB")
    if upright.mode in ("I;16", "I"):  # 16-bit greyscale PNGs
        upright = upright.convert("L")
    alpha = _has_alpha(upright)
    if kind == "PNG" and upright is img:
        png = data
    else:
        buf = io.BytesIO()
        upright.save(buf, "PNG", optimize=False)
        png = buf.getvalue()
    return ImageSource(key, png, "png", upright.width, upright.height, alpha)


# ---------------------------------------------------------------------- #
# items and geometry
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class ImageItem:
    """One placed image.  All lengths are view-space points.

    ``cx``/``cy`` is the centre of the visible (cropped) box; ``width`` and
    ``height`` its size before rotation.  ``rotation`` is clockwise degrees
    on screen.  ``crop`` is in *source* image fractions, independent of flips.
    """

    id: str
    page: int
    source: str
    cx: float
    cy: float
    width: float
    height: float
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    crop: Crop = (0.0, 0.0, 1.0, 1.0)
    opacity: float = 1.0
    border_width: float = 0.0
    border_color: Color = (0, 0, 0)
    corner_radius: float = 0.0
    above_content: bool = True

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def with_(self, **changes: Any) -> ImageItem:
        return dataclasses.replace(self, **changes)

    @property
    def full_size(self) -> tuple[float, float]:
        """Displayed size of the whole (uncropped) source image."""
        left, top, right, bottom = self.crop
        return (self.width / max(right - left, 1e-6), self.height / max(bottom - top, 1e-6))

    def to_params(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data.pop("page")
        data.pop("source")
        data["v"] = _PARAMS_VERSION
        return data

    @classmethod
    def from_params(cls, params: dict[str, Any], *, page: int, source: str) -> ImageItem:
        names = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in params.items() if k in names}
        kwargs["crop"] = tuple(float(v) for v in kwargs.get("crop", (0, 0, 1, 1)))
        kwargs["border_color"] = tuple(int(v) for v in kwargs.get("border_color", (0, 0, 0)))
        kwargs.setdefault("id", cls.new_id())
        return cls(page=page, source=source, **kwargs)


def rotate_vec(x: float, y: float, degrees: float) -> tuple[float, float]:
    """Rotate a vector clockwise on a y-down screen."""
    rad = math.radians(degrees)
    c, s = math.cos(rad), math.sin(rad)
    return (x * c - y * s, x * s + y * c)


def mat_mul(m1: Matrix, m2: Matrix) -> Matrix:
    """``m1`` then ``m2`` (row-vector convention, as PDF/Qt/PyMuPDF)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def mat_invert(m: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return (1.0, 0.0, 0.0, 1.0, -e, -f)
    return (d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det)


def mat_apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def item_matrix(item: ImageItem) -> Matrix:
    """Item-local space -> view space.

    Local space has its origin at the top-left of the visible box, y down,
    one unit per point; flips mirror about the box centre and rotation turns
    about it.  Used verbatim by the on-screen painter and by the exporter.
    """
    rad = math.radians(item.rotation)
    cos, sin = math.cos(rad), math.sin(rad)
    fx = -1.0 if item.flip_h else 1.0
    fy = -1.0 if item.flip_v else 1.0
    a, b = cos * fx, sin * fx
    c, d = -sin * fy, cos * fy
    hw, hh = item.width / 2.0, item.height / 2.0
    return (a, b, c, d, item.cx - a * hw - c * hh, item.cy - b * hw - d * hh)


def display_crop(item: ImageItem) -> Crop:
    """Crop expressed in the flipped (as displayed) orientation."""
    left, top, right, bottom = item.crop
    if item.flip_h:
        left, right = 1.0 - right, 1.0 - left
    if item.flip_v:
        top, bottom = 1.0 - bottom, 1.0 - top
    return (left, top, right, bottom)


def source_crop(item: ImageItem, shown: Crop) -> Crop:
    """Inverse of :func:`display_crop` (the mapping is an involution)."""
    return display_crop(item.with_(crop=shown))


def default_placement(
    source: ImageSource,
    page_size: tuple[float, float],
    *,
    center: tuple[float, float] | None = None,
    fraction: float = 0.6,
) -> tuple[float, float, float, float]:
    """``(cx, cy, w, h)``: natural 96-dpi size, shrunk to fit ``fraction`` of the page."""
    pw, ph = page_size
    w = source.width * 72.0 / _SCREEN_DPI
    h = source.height * 72.0 / _SCREEN_DPI
    scale = min(1.0, fraction * pw / max(w, 1e-6), fraction * ph / max(h, 1e-6))
    w, h = max(MIN_SIZE_PT, w * scale), max(MIN_SIZE_PT, h * scale)
    cx, cy = center if center is not None else (pw / 2.0, ph / 2.0)
    cx = min(max(cx, w / 2.0), pw - w / 2.0) if w <= pw else pw / 2.0
    cy = min(max(cy, h / 2.0), ph - h / 2.0) if h <= ph else ph / 2.0
    return (cx, cy, w, h)


# ---------------------------------------------------------------------- #
# layer + commands
# ---------------------------------------------------------------------- #
@dataclass
class ExportOptions:
    """How images are compressed when baked into the PDF."""

    max_px: int = DEFAULT_MAX_PX  # longest side; 0 disables downscaling
    jpeg_quality: int = DEFAULT_JPEG_QUALITY
    png_lossless: bool = True  # False re-encodes opaque PNG/WebP as JPEG


class ImageLayer:
    """Ordered image items of one document plus their shared sources.

    List order is stacking order; per page the effective order is
    ``[below-content items] < page content < [above-content items]``.
    """

    def __init__(self) -> None:
        self.sources: dict[str, ImageSource] = {}
        self._items: list[ImageItem] = []
        self.options = ExportOptions()

    @property
    def items(self) -> tuple[ImageItem, ...]:
        return tuple(self._items)

    def set_items(self, items: Iterable[ImageItem]) -> None:
        self._items = list(items)

    def add_source(self, source: ImageSource) -> ImageSource:
        return self.sources.setdefault(source.key, source)

    def get(self, item_id: str | None) -> ImageItem | None:
        return next((it for it in self._items if it.id == item_id), None)

    def on_page(self, page: int) -> list[ImageItem]:
        """Items of ``page`` bottom-to-top (below-content group first)."""
        mine = [it for it in self._items if it.page == page]
        return [it for it in mine if not it.above_content] + [
            it for it in mine if it.above_content
        ]

    def has_below(self, page: int) -> bool:
        return any(it.page == page and not it.above_content for it in self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    # --- pure helpers returning new item lists (fed to commands) ----------

    def replaced(self, item: ImageItem) -> list[ImageItem]:
        return [item if it.id == item.id else it for it in self._items]

    def inserted(self, item: ImageItem, after: str | None = None) -> list[ImageItem]:
        items = list(self._items)
        index = next((i + 1 for i, it in enumerate(items) if it.id == after), len(items))
        items.insert(index, item)
        return items

    def removed(self, item_id: str) -> list[ImageItem]:
        return [it for it in self._items if it.id != item_id]

    def restacked(self, item_id: str, how: str) -> list[ImageItem]:
        """Move ``item_id`` ``forward``/``backward``/``front``/``back``.

        Forward from the top of the below-content group crosses the page
        content (becomes above); backward from the bottom of the above group
        sinks behind it.  Front/back go all the way, across the content.
        """
        item = self.get(item_id)
        if item is None:
            return list(self._items)
        stack = self.on_page(item.page)
        pos = stack.index(item)
        others = [it for it in self._items if it.page != item.page]
        if how == "front":
            stack.pop(pos)
            stack.append(item.with_(above_content=True))
        elif how == "back":
            stack.pop(pos)
            stack.insert(0, item.with_(above_content=False))
        elif how == "forward":
            nxt = stack[pos + 1] if pos + 1 < len(stack) else None
            if nxt is not None and nxt.above_content == item.above_content:
                stack[pos], stack[pos + 1] = nxt, item
            elif not item.above_content:
                stack[pos] = item.with_(above_content=True)
        elif how == "backward":
            prv = stack[pos - 1] if pos > 0 else None
            if prv is not None and prv.above_content == item.above_content:
                stack[pos], stack[pos - 1] = prv, item
            elif item.above_content:
                stack[pos] = item.with_(above_content=False)
        return others + stack


class ImageLayerCommand(Command):
    """Swap the layer between two item lists (undoable, no PDF rewrite)."""

    mutates_document = False

    def __init__(
        self,
        description: str,
        before: Sequence[ImageItem],
        after: Sequence[ImageItem],
        *,
        merge_key: str | None = None,
    ) -> None:
        self.description = description
        self._before = tuple(before)
        self._after = tuple(after)
        self._merge_key = merge_key

    def redo(self, session: Any) -> None:
        session.images.set_items(self._after)
        session.mark_dirty()

    def undo(self, session: Any) -> None:
        session.images.set_items(self._before)
        session.mark_dirty()

    def merge_with(self, other: Command) -> bool:
        if (
            isinstance(other, ImageLayerCommand)
            and self._merge_key is not None
            and other._merge_key == self._merge_key
        ):
            self._after = other._after
            return True
        return False


# ---------------------------------------------------------------------- #
# export: encoding
# ---------------------------------------------------------------------- #
def view_to_pdf_matrix(page: pymupdf.Page) -> Matrix:
    """View space (``page.rect``, y down, rotated) -> PDF default user space."""
    m = ~(page.transformation_matrix * page.rotation_matrix)
    return (float(m.a), float(m.b), float(m.c), float(m.d), float(m.e), float(m.f))


def _encode(pdf: pikepdf.Pdf, source: ImageSource, options: ExportOptions) -> pikepdf.Stream:
    """Build the image XObject, downscaling and compressing per ``options``."""
    img: Image.Image = Image.open(io.BytesIO(source.data))
    img.load()
    longest = max(img.width, img.height)
    scale = options.max_px / longest if 0 < options.max_px < longest else 1.0
    passthrough = (
        source.fmt == "jpeg"
        and scale == 1.0
        and img.mode in ("RGB", "L")
        and options.jpeg_quality >= DEFAULT_JPEG_QUALITY
    )
    if passthrough:
        return pdf.make_stream(
            source.data,
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Width=img.width,
            Height=img.height,
            ColorSpace=pikepdf.Name.DeviceGray if img.mode == "L" else pikepdf.Name.DeviceRGB,
            BitsPerComponent=8,
            Filter=pikepdf.Name.DCTDecode,
        )

    alpha: Image.Image | None = None
    if source.has_alpha:
        rgba = img.convert("LA" if img.mode in ("L", "LA") else "RGBA")
        alpha = rgba.getchannel("A")
        img = rgba.convert("L" if rgba.mode == "LA" else "RGB")
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if scale < 1.0:
        size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        img = img.resize(size, Image.Resampling.LANCZOS)
        if alpha is not None:
            alpha = alpha.resize(size, Image.Resampling.LANCZOS)

    colorspace = pikepdf.Name.DeviceGray if img.mode == "L" else pikepdf.Name.DeviceRGB
    as_jpeg = alpha is None and (source.fmt == "jpeg" or not options.png_lossless)
    if as_jpeg:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=max(1, min(100, options.jpeg_quality)), optimize=True)
        data, filt = buf.getvalue(), pikepdf.Name.DCTDecode
    else:
        data, filt = zlib.compress(img.tobytes(), 9), pikepdf.Name.FlateDecode
    stream = pdf.make_stream(
        data,
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Image,
        Width=img.width,
        Height=img.height,
        ColorSpace=colorspace,
        BitsPerComponent=8,
        Filter=filt,
    )
    if alpha is not None:
        stream.SMask = pdf.make_stream(
            zlib.compress(alpha.tobytes(), 9),
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Width=alpha.width,
            Height=alpha.height,
            ColorSpace=pikepdf.Name.DeviceGray,
            BitsPerComponent=8,
            Filter=pikepdf.Name.FlateDecode,
        )
    return stream


# ---------------------------------------------------------------------- #
# export: content
# ---------------------------------------------------------------------- #
def _num(value: float) -> str:
    text = f"{value:.5f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _rounded_rect_ops(x: float, y: float, w: float, h: float, r: float) -> str:
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    if r <= 0.0:
        return f"{_num(x)} {_num(y)} {_num(w)} {_num(h)} re"
    k = r * _KAPPA
    x1, y1 = x + w, y + h
    n = _num
    return " ".join(
        [
            f"{n(x + r)} {n(y)} m",
            f"{n(x1 - r)} {n(y)} l",
            f"{n(x1 - r + k)} {n(y)} {n(x1)} {n(y + r - k)} {n(x1)} {n(y + r)} c",
            f"{n(x1)} {n(y1 - r)} l",
            f"{n(x1)} {n(y1 - r + k)} {n(x1 - r + k)} {n(y1)} {n(x1 - r)} {n(y1)} c",
            f"{n(x + r)} {n(y1)} l",
            f"{n(x + r - k)} {n(y1)} {n(x)} {n(y1 - r + k)} {n(x)} {n(y1 - r)} c",
            f"{n(x)} {n(y + r)} l",
            f"{n(x)} {n(y + r - k)} {n(x + r - k)} {n(y)} {n(x + r)} {n(y)} c",
            "h",
        ]
    )


def _item_ops(item: ImageItem, view_to_pdf: Matrix, image_name: str, gs_name: str | None) -> str:
    cm = mat_mul(item_matrix(item), view_to_pdf)
    full_w, full_h = item.full_size
    x0 = -item.crop[0] * full_w
    y0 = -item.crop[1] * full_h
    params = json.dumps(item.to_params(), separators=(",", ":")).encode().hex()
    lines = [
        f"{_MARK_IMAGE} <</P <{params}>>> BDC",
        "q",
        " ".join(_num(v) for v in cm) + " cm",
    ]
    if gs_name is not None:
        lines.append(f"/{gs_name} gs")
    lines += [
        "q",
        _rounded_rect_ops(0.0, 0.0, item.width, item.height, item.corner_radius) + " W n",
        f"{_num(full_w)} 0 0 {_num(-full_h)} {_num(x0)} {_num(y0 + full_h)} cm",
        f"/{image_name} Do",
        "Q",
    ]
    bw = min(item.border_width, item.width / 2.0, item.height / 2.0)
    if bw > 0:
        r, g, b = (c / 255.0 for c in item.border_color)
        half = bw / 2.0
        path = _rounded_rect_ops(
            half, half, item.width - bw, item.height - bw, max(0.0, item.corner_radius - half)
        )
        lines.append(f"{_num(r)} {_num(g)} {_num(b)} RG {_num(bw)} w 0 j {path} S")
    lines += ["Q", "EMC"]
    return "\n".join(lines) + "\n"


def _own_resources(page: pikepdf.Page) -> pikepdf.Dictionary:
    """Page /Resources, copying inherited ones down first so nothing is lost."""
    obj = page.obj
    if "/Resources" not in obj:
        node: Any = obj
        inherited = None
        while node is not None and inherited is None:
            inherited = node.get("/Resources")
            node = node.get("/Parent")
        obj.Resources = pikepdf.Dictionary(dict(inherited.items())) if inherited else (
            pikepdf.Dictionary()
        )
    return cast(pikepdf.Dictionary, obj.Resources)


def _free_name(container: Any, prefix: str, start: int) -> tuple[str, int]:
    index = start
    while container is not None and f"/{prefix}{index}" in container:
        index += 1
    return f"{prefix}{index}", index + 1


def bake_images(
    pdf: pikepdf.Pdf,
    view: pymupdf.Document,
    layer: ImageLayer,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Draw every layer item into ``pdf`` (mutated in place).

    ``view`` is the PyMuPDF rendering of the same pages and provides the
    exact view->PDF matrix (CropBox offset and /Rotate included).  Returns
    the number of images written.
    """
    encoded: dict[str, pikepdf.Object] = {}
    pages = sorted({it.page for it in layer.items if 0 <= it.page < len(pdf.pages)})
    written = 0
    for done, page_index in enumerate(pages):
        page = pdf.pages[page_index]
        to_pdf = view_to_pdf_matrix(view.load_page(page_index))
        resources = _own_resources(page)
        if "/XObject" not in resources:
            resources.XObject = pikepdf.Dictionary()
        if "/ExtGState" not in resources:
            resources.ExtGState = pikepdf.Dictionary()
        below: list[str] = []
        above: list[str] = []
        im_next = gs_next = 0
        for item in layer.on_page(page_index):
            source = layer.sources.get(item.source)
            if source is None:
                logger.warning("image %s has no source; skipped", item.id)
                continue
            if source.key not in encoded:
                encoded[source.key] = pdf.make_indirect(_encode(pdf, source, layer.options))
            image_name, im_next = _free_name(resources.XObject, _RES_PREFIX_IMAGE, im_next)
            resources.XObject[f"/{image_name}"] = encoded[source.key]
            gs_name = None
            if item.opacity < 0.999:
                gs_name, gs_next = _free_name(resources.ExtGState, _RES_PREFIX_GS, gs_next)
                alpha = round(max(0.0, min(1.0, item.opacity)), 4)
                resources.ExtGState[f"/{gs_name}"] = pikepdf.Dictionary(
                    Type=pikepdf.Name.ExtGState, ca=alpha, CA=alpha
                )
            ops = _item_ops(item, to_pdf, image_name, gs_name)
            (above if item.above_content else below).append(ops)
            written += 1
        if above:
            # Isolate the original content's graphics state from the overlay.
            page.contents_add(f"{_MARK_WRAP} BMC\nq\n".encode(), prepend=True)
            page.contents_add(b"\nQ\nEMC\n" + "".join(above).encode())
        if below:
            page.contents_add("".join(below).encode(), prepend=True)
        if progress is not None:
            progress(done + 1, len(pages))
    return written


# ---------------------------------------------------------------------- #
# reopen: lifting baked images back into the layer
# ---------------------------------------------------------------------- #
def _source_from_xobject(xobj: Any) -> ImageSource:
    if xobj.get("/Filter") == pikepdf.Name.DCTDecode and "/SMask" not in xobj:
        data = xobj.read_raw_bytes()
        return load_image_source(data)
    img = pikepdf.PdfImage(xobj).as_pil_image()
    smask = xobj.get("/SMask")
    if smask is not None:
        mask = pikepdf.PdfImage(smask).as_pil_image().convert("L")
        img = img.convert("RGBA")
        img.putalpha(mask.resize(img.size))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return load_image_source(buf.getvalue())


def _has_baked(page: pikepdf.Page) -> bool:
    resources = page.obj.get("/Resources")
    xobjects = resources.get("/XObject") if resources is not None else None
    return xobjects is not None and any(
        str(name).startswith(f"/{_RES_PREFIX_IMAGE}") for name in xobjects.keys()  # noqa: SIM118
    )


def lift_images(pdf: pikepdf.Pdf) -> ImageLayer:
    """Remove baked image blocks from ``pdf`` and return them as a layer.

    Only pages carrying ``OPDFIm*`` resources are parsed, so documents that
    never had images inserted pay nothing.  Unknown or damaged blocks are
    left untouched in the content stream.
    """
    layer = ImageLayer()
    by_object: dict[tuple[int, int], str] = {}
    items: list[ImageItem] = []
    for page_index, page in enumerate(pdf.pages):
        if not _has_baked(page):
            continue
        xobjects = page.obj.Resources.XObject
        try:
            instructions = pikepdf.parse_content_stream(page)
        except pikepdf.PdfError:
            logger.warning("page %d: content unreadable, images left baked", page_index + 1)
            continue
        kept: list[Any] = []
        lifted: list[ImageItem] = []
        stack: list[str] = []  # marked-content tags, innermost last
        skipping: list[Any] | None = None
        drop_next_q = False
        for inst in instructions:
            op = str(getattr(inst, "operator", ""))
            operands = list(getattr(inst, "operands", []))
            if skipping is not None:
                skipping.append(inst)
                if op in ("BMC", "BDC"):
                    stack.append("")
                elif op == "EMC":
                    stack.pop()
                    if not stack or stack[-1] != _MARK_IMAGE:
                        continue
                    stack.pop()
                    item = _parse_block(skipping, xobjects, page_index, layer, by_object)
                    if item is None:
                        kept.extend(skipping)
                    else:
                        lifted.append(item)
                    skipping = None
                continue
            if op == "BDC" and operands and str(operands[0]) == _MARK_IMAGE:
                stack.append(_MARK_IMAGE)
                stack.append("")  # the block's own level, closed by its EMC
                skipping = [inst]
                continue
            if op == "BMC" and operands and str(operands[0]) == _MARK_WRAP:
                stack.append(_MARK_WRAP)
                drop_next_q = True
                continue
            if drop_next_q and op == "q":
                drop_next_q = False
                continue
            drop_next_q = False
            if op in ("BMC", "BDC"):
                stack.append(str(operands[0]) if operands else "")
            elif op == "EMC" and stack and stack.pop() == _MARK_WRAP:
                if kept and str(getattr(kept[-1], "operator", "")) == "Q":
                    kept.pop()
                continue
            kept.append(inst)
        if not lifted:
            continue
        page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(kept))
        resources = page.obj.Resources
        for group, prefix in (("/XObject", _RES_PREFIX_IMAGE), ("/ExtGState", _RES_PREFIX_GS)):
            container = resources.get(group)
            if container is None:
                continue
            for name in [n for n in container.keys() if str(n).startswith(f"/{prefix}")]:  # noqa: SIM118
                del container[name]
        items.extend(lifted)
    # Restore stacking: below-content blocks precede above ones per page.
    layer.set_items(sorted(items, key=lambda it: (it.page, it.above_content)))
    if items:
        logger.info("lifted %d inserted images for editing", len(items))
    return layer


def _parse_block(
    block: list[Any],
    xobjects: Any,
    page_index: int,
    layer: ImageLayer,
    by_object: dict[tuple[int, int], str],
) -> ImageItem | None:
    try:
        props = block[0].operands[1]
        params = json.loads(bytes(props.P).decode("utf-8"))
        name = next(
            str(inst.operands[0])
            for inst in block
            if str(getattr(inst, "operator", "")) == "Do"
        )
        xobj = xobjects[name]
        objgen = xobj.objgen
        key = by_object.get(objgen)
        if key is None:
            source = layer.add_source(_source_from_xobject(xobj))
            key = source.key
            if objgen != (0, 0):
                by_object[objgen] = key
        return ImageItem.from_params(params, page=page_index, source=key)
    except Exception:
        logger.warning("page %d: unreadable image block kept as content", page_index + 1)
        return None
