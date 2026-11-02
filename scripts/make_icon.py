"""Generate the application icon and installer artwork (development time only).

Everything is drawn with Pillow from the vendored Noto Sans fonts, so the output
is reproducible offline.  The results are committed; re-run this script only
when the artwork changes.

Outputs:

* ``assets/app.ico`` - multi-size application icon; small sizes use a
  simplified drawing so they stay crisp.
* ``assets/installer/wizard-*.bmp`` - side panel of the setup wizard's
  welcome and finish pages, one file per DPI scale.
* ``assets/installer/small-*.bmp`` - header image of the inner wizard pages.

Usage::

    python scripts/make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT_BOLD = ROOT / "assets" / "fonts" / "NotoSans" / "NotoSans-Bold.ttf"
FONT_REGULAR = ROOT / "assets" / "fonts" / "NotoSans" / "NotoSans-Regular.ttf"
OUT = ROOT / "assets" / "app.ico"
OUT_ICNS = ROOT / "assets" / "app.icns"
INSTALLER = ROOT / "assets" / "installer"

SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
MASTER = 1024
# Inno Setup picks the closest size for the current DPI (100 %, 150 %, 200 %).
WIZARD_SIZES = [(164, 314), (246, 471), (328, 628)]
SMALL_SIZES = [55, 83, 110]

TILE_TOP = (238, 72, 62)
TILE_BOTTOM = (176, 22, 30)
PAGE = (255, 255, 255, 255)
FOLD = (250, 205, 200, 255)
LINES = (214, 219, 227, 255)
PEN_BODY = (33, 38, 48, 255)
PEN_BAND = (150, 158, 172, 255)
PEN_WOOD = (255, 196, 92, 255)
PEN_LEAD = (33, 38, 48, 255)
WIZ_TOP = (204, 32, 40)
WIZ_BOTTOM = (112, 10, 18)


def _vertical_gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    w, h = size
    column = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        column.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)))
    return column.resize((w, h))


def _shadow(shape: Image.Image, offset: tuple[int, int], blur: float, alpha: int) -> Image.Image:
    """Soft drop shadow for the opaque parts of ``shape``."""
    mask = shape.getchannel("A").point(lambda a: a * alpha // 255)
    shadow = Image.new("RGBA", shape.size, (40, 0, 0, 0))
    shadow.putalpha(mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    out = Image.new("RGBA", shape.size, (0, 0, 0, 0))
    out.paste(shadow, offset, shadow)
    return out


def draw_glyph(size: int, detail: int) -> Image.Image:
    """White page with a folded corner and a pen, on a transparent square.

    ``detail``: 0 = page only, 1 = page and pen, 2 = page, text lines and pen.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256

    left, top, right, bottom, fold = 52 * s, 28 * s, 190 * s, 228 * s, 44 * s
    d.rounded_rectangle([left, top, right, bottom], radius=12 * s, fill=PAGE)
    # cut the folded corner out, then draw the fold itself
    d.polygon([(right - fold, top - 1), (right + 1, top - 1), (right + 1, top + fold)], fill=(0, 0, 0, 0))
    d.polygon([(right - fold, top), (right - fold, top + fold - 8 * s), (right, top + fold)], fill=PAGE)
    d.polygon([(right - fold, top), (right - fold + 8 * s, top + fold), (right, top + fold)], fill=FOLD)
    d.polygon([(right - fold, top), (right - fold, top + fold - 8 * s), (right - fold + 8 * s, top + fold)], fill=FOLD)

    if detail >= 2:
        for i, end in enumerate((150, 172, 172, 122)):
            y = (88 + i * 26) * s
            d.rounded_rectangle([left + 24 * s, y, end * s, y + 11 * s], radius=5.5 * s, fill=LINES)

    if detail >= 1:
        pen = _draw_pen(size)
        img.alpha_composite(_shadow(pen, (round(4 * s), round(6 * s)), 6 * s, 110))
        img.alpha_composite(pen)
    return img


def _draw_pen(size: int) -> Image.Image:
    """Pencil lying at 45 degrees, tip towards the lower left of the page."""
    s = size / 256
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    angle = math.radians(-45)
    tip = (112 * s, 214 * s)
    ux, uy = math.cos(angle), math.sin(angle)  # along the pen, from tip to cap
    vx, vy = -uy, ux  # across the pen
    half = 15 * s

    def quad(a: float, b: float, w: float) -> list[tuple[float, float]]:
        return [
            (tip[0] + ux * a + vx * w, tip[1] + uy * a + vy * w),
            (tip[0] + ux * b + vx * w, tip[1] + uy * b + vy * w),
            (tip[0] + ux * b - vx * w, tip[1] + uy * b - vy * w),
            (tip[0] + ux * a - vx * w, tip[1] + uy * a - vy * w),
        ]

    def point(a: float, w: float = 0.0) -> tuple[float, float]:
        return (tip[0] + ux * a + vx * w, tip[1] + uy * a + vy * w)

    length = 150 * s
    cone = 34 * s
    d.polygon([point(0), point(cone, half), point(cone, -half)], fill=PEN_WOOD)
    d.polygon([point(0), point(cone * 0.32, half * 0.32), point(cone * 0.32, -half * 0.32)], fill=PEN_LEAD)
    d.polygon(quad(cone, length - 22 * s, half), fill=PEN_BODY)
    d.polygon(quad(length - 22 * s, length - 12 * s, half), fill=PEN_BAND)
    d.polygon(quad(length - 12 * s, length, half), fill=(225, 70, 70, 255))
    # highlight along the body
    d.polygon(quad(cone + 4 * s, length - 26 * s, half * 0.25), fill=(70, 78, 94, 255))
    return layer


def draw_icon(size: int) -> Image.Image:
    """App icon at ``size`` px, drawn 4x larger and downsampled for smooth edges."""
    big = size * 4 if size < 256 else MASTER
    detail = 0 if size <= 20 else 1 if size <= 48 else 2
    s = big / 256

    tile_mask = Image.new("L", (big, big), 0)
    inset = 6 * s if size > 20 else 0
    ImageDraw.Draw(tile_mask).rounded_rectangle([inset, inset, big - inset, big - inset], radius=56 * s, fill=255)
    tile = _vertical_gradient((big, big), TILE_TOP, TILE_BOTTOM).convert("RGBA")
    tile.putalpha(tile_mask)

    # soft highlight on the upper half of the tile
    gloss = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(gloss).ellipse([-60 * s, -190 * s, 316 * s, 120 * s], fill=(255, 255, 255, 28))
    gloss = gloss.filter(ImageFilter.GaussianBlur(14 * s))
    gloss.putalpha(Image.composite(gloss.getchannel("A"), Image.new("L", (big, big), 0), tile_mask))
    tile.alpha_composite(gloss)

    glyph = draw_glyph(big, detail)
    if detail == 0:
        # tiny sizes: a larger page with no shadow reads better
        glyph = glyph.resize((round(big * 1.12),) * 2, Image.Resampling.LANCZOS)
        glyph = glyph.crop((round(big * 0.06),) * 2 + (round(big * 1.06),) * 2)
    else:
        tile.alpha_composite(_shadow(glyph, (0, round(5 * s)), 7 * s, 90))
    tile.alpha_composite(glyph)
    return tile.resize((size, size), Image.Resampling.LANCZOS)


def draw_wizard(size: tuple[int, int]) -> Image.Image:
    """Tall side panel for the welcome / finish pages."""
    w, h = size
    k = 2  # supersample
    W, H = w * k, h * k
    s = W / 164
    img = _vertical_gradient((W, H), WIZ_TOP, WIZ_BOTTOM).convert("RGBA")

    # faint oversized page outlines as a background texture
    deco = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(deco)
    for cx, cy, sz in ((150, 40, 110), (-10, 250, 130), (120, 300, 70)):
        x0, y0 = (cx - sz * 0.38) * s, (cy - sz / 2) * s
        dd.rounded_rectangle([x0, y0, x0 + sz * 0.76 * s, y0 + sz * s], radius=8 * s,
                             outline=(255, 255, 255, 22), width=round(2.5 * s))
    img.alpha_composite(deco.rotate(-12, resample=Image.Resampling.BICUBIC))

    glyph = draw_glyph(round(104 * s), 2)
    gx, gy = (W - glyph.width) // 2, round(70 * s)
    placed = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    placed.paste(glyph, (gx, gy))
    img.alpha_composite(_shadow(placed, (0, round(6 * s)), 8 * s, 120))
    img.alpha_composite(placed)

    d = ImageDraw.Draw(img)
    title = ImageFont.truetype(str(FONT_BOLD), round(17 * s))
    sub = ImageFont.truetype(str(FONT_REGULAR), round(9.5 * s))
    cx = W / 2
    d.text((cx, 205 * s), "OfflinePDF", font=title, fill=(255, 255, 255), anchor="mm")
    d.text((cx, 226 * s), "STUDIO", font=ImageFont.truetype(str(FONT_BOLD), round(10 * s)),
           fill=(255, 214, 210), anchor="mm")
    d.line([(cx - 18 * s, 244 * s), (cx + 18 * s, 244 * s)], fill=(255, 255, 255, 110), width=max(1, round(s)))
    for i, line in enumerate(("Edit PDFs with ease.", "100% offline & private.")):
        d.text((cx, (262 + i * 15) * s), line, font=sub, fill=(255, 225, 222), anchor="mm")
    return img.convert("RGB").resize((w, h), Image.Resampling.LANCZOS)


def draw_small(size: int) -> Image.Image:
    """Header image of the inner wizard pages (white background)."""
    img = Image.new("RGB", (size, size), (255, 255, 255))
    icon = draw_icon(size)
    img.paste(icon, (0, 0), icon)
    return img


def main() -> int:
    icons = [draw_icon(n) for n in SIZES]
    largest = icons[-1]
    largest.save(OUT, format="ICO", sizes=[(n, n) for n in SIZES], append_images=icons[:-1])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, sizes {SIZES})")

    ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]
    icns_images = [draw_icon(n) for n in ICNS_SIZES]
    icns_images[0].save(OUT_ICNS, format="ICNS", append_images=icns_images[1:])
    print(f"wrote {OUT_ICNS} ({OUT_ICNS.stat().st_size} bytes, sizes {ICNS_SIZES})")

    INSTALLER.mkdir(exist_ok=True)
    largest.save(INSTALLER / "icon-preview.png")
    for w, h in WIZARD_SIZES:
        path = INSTALLER / f"wizard-{w}x{h}.bmp"
        draw_wizard((w, h)).save(path)
        print(f"wrote {path}")
    for n in SMALL_SIZES:
        path = INSTALLER / f"small-{n}.bmp"
        draw_small(n).save(path)
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
