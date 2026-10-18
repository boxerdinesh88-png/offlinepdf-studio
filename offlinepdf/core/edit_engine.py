"""Single-line, in-place text editing (Milestone M3).

The engine rewrites the text-showing operators of one span inside the page
(or form XObject) content stream and leaves graphics state, clipping and
marked content untouched.  When the shows span several streams it falls back
to blanking the original operators and appending a self-contained block at
the same baseline origin.  Fonts come from
:class:`~offlinepdf.core.font_resolver.FontResolver`: either the original
resource is reused (100%) or a glyph subset of a bundled/system font is
embedded with a fresh ToUnicode map.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import logging
import math
import re
import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as _dc_replace
from functools import lru_cache
from typing import Any

import pikepdf
import pymupdf
from fontTools import subset as ft_subset
from fontTools.agl import toUnicode as agl_to_unicode
from fontTools.ttLib import TTFont

from offlinepdf.core.content_stream import (
    IDENTITY,
    ColorValue,
    FontRef,
    Matrix,
    Point,
    ShowEvent,
    mat_mul,
    mat_scale,
)
from offlinepdf.core.document import PdfSession
from offlinepdf.core.font_program import (
    FontProgram,
    advance_widths,
    codepoint_coverage,
    present_codes,
    program_bytes,
)
from offlinepdf.core.font_resolver import (
    STRATEGY_REUSE,
    FontDecision,
    FontRequest,
    FontResolver,
)
from offlinepdf.core.history import Command
from offlinepdf.core.inspect_session import InspectSession
from offlinepdf.core.layout import (
    Alignment,
    BlockFrame,
    FlowBlock,
    FlowRun,
    OverflowMode,
    RunStyle,
    align_lines,
    apply_auto_fit,
    block_alignment,
    flow_bbox_model,
    intersect,
    wrap_paragraph,
)
from offlinepdf.core.text_model import PageModel, TextSpan, _page_top

logger = logging.getLogger(__name__)

__all__ = ["EditEngine", "EditResult"]

_SHOW_OPS = frozenset({"Tj", "TJ", "'", '"'})
_FONT_PREFIX = "PdfEditF"
_BASE14_PYMUPDF = (
    ("helv", "helv"),
    ("tiro", "tiro"),
    ("couri", "cour"),
    ("symbol", "symb"),
    ("zapf", "zadb"),
)


class _Reject(Exception):
    """Internal: the edit was refused (carries the public result)."""

    def __init__(self, result: EditResult) -> None:
        super().__init__(result.warning)
        self.result = result


class _PlanError(Exception):
    """Internal: the prepared rewrite could not be built."""


# --------------------------------------------------------------------------- #
# encoding: writing new text with the original font resource
# --------------------------------------------------------------------------- #
_WINANSI_EXTRA: dict[int, int] = {
    0x20AC: 0x80, 0x201A: 0x82, 0x0192: 0x83, 0x201E: 0x84, 0x2026: 0x85,
    0x2020: 0x86, 0x2021: 0x87, 0x02C6: 0x88, 0x2030: 0x89, 0x0160: 0x8A,
    0x2039: 0x8B, 0x0152: 0x8C, 0x017D: 0x8E, 0x2018: 0x91, 0x2019: 0x92,
    0x201C: 0x93, 0x201D: 0x94, 0x2022: 0x95, 0x2013: 0x96, 0x2014: 0x97,
    0x02DC: 0x98, 0x2122: 0x99, 0x0161: 0x9A, 0x203A: 0x9B, 0x0153: 0x9C,
    0x017E: 0x9E, 0x0178: 0x9F,
}


@lru_cache(maxsize=8)
def _base_table(name: str) -> dict[int, int] | None:
    if name == "WinAnsiEncoding":
        table = {cp: cp for cp in range(0x20, 0x7F)}
        table.update({cp: cp for cp in range(0xA0, 0x100)})
        table.update(_WINANSI_EXTRA)
        return table
    if name == "StandardEncoding":
        from fontTools.encodings.StandardEncoding import StandardEncoding

        table = {}
        for code, glyph_name in enumerate(StandardEncoding):
            if not glyph_name or glyph_name == ".notdef":
                continue
            try:
                uni = agl_to_unicode(glyph_name)
            except Exception:
                continue
            if len(uni) == 1:
                table[ord(uni)] = code
        return table
    if name == "MacRomanEncoding":
        table = {}
        for code in range(32, 256):
            try:
                ch = bytes((code,)).decode("mac_roman")
            except UnicodeDecodeError:
                continue
            if len(ch) == 1 and ch.isprintable():
                table[ord(ch)] = code
        return table
    return None


def _differences_table(font_dict: pikepdf.Dictionary | None) -> dict[int, int]:
    if font_dict is None:
        return {}
    encoding = font_dict.get("/Encoding")
    if not isinstance(encoding, pikepdf.Dictionary):
        return {}
    differences = encoding.get("/Differences")
    if not isinstance(differences, pikepdf.Array):
        return {}
    table: dict[int, int] = {}
    code = 0
    for item in differences:
        if isinstance(item, pikepdf.Name):
            glyph_name = str(item).lstrip("/")
            if glyph_name and glyph_name != ".notdef":
                try:
                    uni = agl_to_unicode(glyph_name)
                except Exception:
                    uni = ""
                if len(uni) == 1:
                    table[ord(uni)] = code
            code += 1
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            code = int(item)
    return table


def _candidate_tables(font_dict: pikepdf.Dictionary | None) -> list[dict[int, int]]:
    encoding = font_dict.get("/Encoding") if font_dict is not None else None
    tables: list[dict[int, int]] = []
    if isinstance(encoding, pikepdf.Name):
        table = _base_table(str(encoding).lstrip("/"))
        if table is not None:
            tables.append(table)
    elif isinstance(encoding, pikepdf.Dictionary):
        base = encoding.get("/BaseEncoding")
        names = (
            [str(base).lstrip("/")]
            if isinstance(base, pikepdf.Name)
            else ["WinAnsiEncoding", "StandardEncoding", "MacRomanEncoding"]
        )
        for name in names:
            table = _base_table(name)
            if table is not None:
                tables.append(table)
    else:
        for name in ("WinAnsiEncoding", "StandardEncoding", "MacRomanEncoding"):
            table = _base_table(name)
            if table is not None:
                tables.append(table)
    differences = _differences_table(font_dict)
    if differences:
        if tables:
            tables = [{**table, **differences} for table in tables]
        else:
            tables = [dict(differences)]
    return tables


def _encode_by_tounicode(
    text: str, font_dict: pikepdf.Dictionary | None, width: int
) -> tuple[bytes, dict[str, bytes], dict[int, int]] | None:
    """Encode with the inverse of the font's ToUnicode map (subset fonts).

    Subset fonts often use a private code order (no /Encoding, or a cmap
    keyed by codes), so the standard tables would pick the wrong glyphs;
    ToUnicode is what the file itself says each code shows.
    """
    if font_dict is None:
        return None
    inverse = present_codes(font_dict)
    if not inverse:
        return None
    codes: dict[str, bytes] = {}
    for char in text:
        code = inverse.get(ord(char))
        if code is None or len(code) != width:
            return None
        codes[char] = code
    table = {cp: int.from_bytes(code, "big") for cp, code in inverse.items() if len(code) == 1}
    return b"".join(codes[char] for char in text), codes, table


def _encode_simple(
    text: str,
    font_dict: pikepdf.Dictionary | None,
    coverage: frozenset[int] | None,
) -> tuple[bytes, dict[str, bytes], dict[int, int]] | None:
    by_map = _encode_by_tounicode(text, font_dict, 1)
    if by_map is not None:
        return by_map
    tables = _candidate_tables(font_dict)
    for table in tables:
        codes: dict[str, bytes] = {}
        ok = True
        for char in text:
            cp = ord(char)
            if coverage is not None and cp not in coverage:
                ok = False
                break
            code = table.get(cp)
            if code is None or code == 0:
                ok = False
                break
            codes[char] = bytes((code,))
        if ok:
            return b"".join(codes[char] for char in text), codes, table
    return None


def _descendant(font_dict: pikepdf.Dictionary) -> pikepdf.Dictionary | None:
    children = font_dict.get("/DescendantFonts")
    if isinstance(children, pikepdf.Array) and len(children) > 0:
        first = children[0]
        if isinstance(first, pikepdf.Dictionary):
            return first
    return None


def _encode_type0(
    text: str,
    font_dict: pikepdf.Dictionary,
    prog_bytes: bytes | None,
    kind: str,
    coverage: frozenset[int] | None,
) -> tuple[bytes, dict[str, bytes], dict[int, int]] | None:
    encoding = font_dict.get("/Encoding")
    if not isinstance(encoding, pikepdf.Name) or "Identity" not in str(encoding):
        return None
    desc = _descendant(font_dict)
    if desc is None or str(desc.get("/Subtype") or "") != "/CIDFontType2":
        return None
    if kind != "ttf" or prog_bytes is None:
        return None
    if coverage is not None and any(ord(char) not in coverage for char in text):
        return _encode_by_tounicode(text, font_dict, 2)
    try:
        font = TTFont(io.BytesIO(prog_bytes), lazy=True)
        # Subset programs often have no cmap table at all (getBestCmap raises).
        cmap = (font.getBestCmap() or {}) if "cmap" in font else {}
        gid_of = {name: index for index, name in enumerate(font.getGlyphOrder())}
    except Exception:
        return _encode_by_tounicode(text, font_dict, 2)
    reverse: dict[int, int] = {}
    id_pairs: tuple[int, ...] | None = None
    cid_to_gid = desc.get("/CIDToGIDMap")
    if isinstance(cid_to_gid, pikepdf.Stream):
        raw = cid_to_gid.read_bytes()
        count = len(raw) // 2
        id_pairs = struct.unpack(f">{count}H", raw[: count * 2])
        for pair_cid, pair_gid in enumerate(id_pairs):
            reverse.setdefault(pair_gid, pair_cid)
    codes: dict[str, bytes] = {}
    if not cmap:  # subset program with its cmap stripped
        font.close()
        return _encode_by_tounicode(text, font_dict, 2)
    try:
        for char in text:
            glyph_name = cmap.get(ord(char))
            if glyph_name is None:
                return _encode_by_tounicode(text, font_dict, 2)
            found_gid = gid_of.get(glyph_name)
            if found_gid is None:
                return None
            cid = reverse.get(found_gid) if id_pairs is not None else found_gid
            if cid is None:
                return None
            codes[char] = cid.to_bytes(2, "big")
    finally:
        font.close()
    return b"".join(codes[char] for char in text), codes, {}


def _pdf_char_widths(
    font_dict: pikepdf.Dictionary | None, codes: dict[str, bytes]
) -> dict[str, int] | None:
    """Advance widths (1/1000 em) the PDF itself declares for these codes.

    /Widths (simple fonts) and /W (CID fonts) are what viewers use to place
    the glyphs, and unlike the program's cmap they work for subset fonts.
    """
    if font_dict is None or not codes:
        return None
    try:
        out: dict[str, int] = {}
        if str(font_dict.get("/Subtype") or "") == "/Type0":
            desc = _descendant(font_dict)
            if desc is None:
                return None
            default = float(desc.get("/DW", 1000))
            table: dict[int, float] = {}
            spec = list(desc.get("/W", pikepdf.Array()))
            i = 0
            while i < len(spec):
                first = int(spec[i])
                if i + 1 < len(spec) and isinstance(spec[i + 1], pikepdf.Array):
                    for offset, item in enumerate(spec[i + 1]):
                        table[first + offset] = float(item)
                    i += 2
                elif i + 2 < len(spec):
                    last, run_width = int(spec[i + 1]), float(spec[i + 2])
                    for cid in range(first, last + 1):
                        table[cid] = run_width
                    i += 3
                else:
                    break
            for char, code in codes.items():
                out[char] = round(table.get(int.from_bytes(code, "big"), default))
            return out
        widths = font_dict.get("/Widths")
        first_char = int(font_dict.get("/FirstChar", 0))
        if not isinstance(widths, pikepdf.Array):
            return None
        for char, code in codes.items():
            index = int.from_bytes(code, "big") - first_char
            if not 0 <= index < len(widths):
                return None
            out[char] = round(float(widths[index]))
        return out
    except (TypeError, ValueError):
        return None


def _encode_original(
    text: str,
    ref: FontRef,
    font_dict: pikepdf.Dictionary | None,
    prog_bytes: bytes | None,
    kind: str,
) -> tuple[bytes, dict[str, bytes], dict[int, int]] | None:
    coverage = None
    if prog_bytes and kind:
        coverage = codepoint_coverage(prog_bytes, kind)
    if ref.subtype == "Type0":
        if font_dict is None:
            return None
        return _encode_type0(text, font_dict, prog_bytes, kind, coverage)
    return _encode_simple(text, font_dict, coverage)


# --------------------------------------------------------------------------- #
# embedding: subset of a bundled/system font, Type0 + Identity-H
# --------------------------------------------------------------------------- #
@dataclass
class _Embedded:
    data: bytes
    ps_name: str
    char_bytes: dict[str, bytes]
    char_width: dict[str, int]
    flags: int
    bbox: tuple[int, int, int, int]
    ascent: int
    descent: int
    cap_height: int
    italic_angle: int
    stem_v: int
    cid_to_gid: list[int]
    widths: list[int]


def _load_font(source: str | bytes, face: int) -> TTFont | None:
    try:
        if isinstance(source, bytes):
            return TTFont(io.BytesIO(source))
        return TTFont(source, fontNumber=face)
    except Exception:
        return None


def _embed_program(source: str | bytes, face: int, text: str) -> _Embedded | None:
    font = _load_font(source, face)
    if font is None:
        return None
    data = b""
    try:
        if getattr(font, "sfntVersion", "") == "OTTO":
            return None
        options = ft_subset.Options()
        options.retain_gids = False
        options.notdef_outline = True
        options.recalc_bounds = True
        options.recalc_timestamp = False
        subsetter = ft_subset.Subsetter(options=options)
        subsetter.populate(text=text)
        subsetter.subset(font)
        buffer = io.BytesIO()
        font.save(buffer)
        data = buffer.getvalue()
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            font.close()
    if data[:4] == b"OTTO":
        return None
    return _embedded_metrics(data, text)


def _embedded_metrics(data: bytes, text: str) -> _Embedded | None:
    font = _load_font(data, 0)
    if font is None:
        return None
    try:
        cmap = font.getBestCmap() or {}
        chars = list(dict.fromkeys(text))
        if any(ord(char) not in cmap for char in chars):
            return None
        hmtx = font["hmtx"]
        upm = int(font["head"].unitsPerEm or 1000)
        scale = 1000.0 / upm
        char_bytes: dict[str, bytes] = {}
        cid_to_gid = [0]
        widths = [0]
        for cid, char in enumerate(chars, start=1):
            glyph_name = cmap[ord(char)]
            glyph_id = font.getGlyphID(glyph_name)
            advance = hmtx.metrics.get(glyph_name, (upm // 2, 0))[0]
            char_bytes[char] = cid.to_bytes(2, "big")
            cid_to_gid.append(glyph_id)
            widths.append(round(advance * scale, 2))
        head = font["head"]
        bbox = (
            round(head.xMin * scale),
            round(head.yMin * scale),
            round(head.xMax * scale),
            round(head.yMax * scale),
        )
        hhea = font["hhea"]
        ascent = round(hhea.ascent * scale)
        descent = round(hhea.descent * scale)
        os2 = font.get("OS/2")
        cap_value = int(getattr(os2, "sCapHeight", 0) or 0) if os2 else 0
        cap_height = round(cap_value * scale) if cap_value else max(ascent, 400)
        italic_angle = round(float(getattr(font["post"], "italicAngle", 0.0) or 0.0))
        weight = int(getattr(os2, "usWeightClass", 400) or 400) if os2 else 400
        flags = 64
        if italic_angle:
            flags |= 1
        family_class = int(getattr(os2, "sFamilyClass", 0) or 0) if os2 else 0
        if 8 <= family_class <= 10:
            flags |= 2
        if getattr(font["post"], "isFixedPitch", 0):
            flags |= 4
        stem_v = 140 if weight >= 600 else 80
        ps_name = (font["name"].getDebugName(6) or "EmbeddedFont").replace(" ", "")
    except Exception:
        with contextlib.suppress(Exception):
            font.close()
        return None
    with contextlib.suppress(Exception):
        font.close()
    cps = [ord(char) for char in char_bytes]
    known = advance_widths(data, "ttf", cps)
    char_width = {char: known[ord(char)] for char in char_bytes if ord(char) in known}
    return _Embedded(
        data=data,
        ps_name=ps_name,
        char_bytes=char_bytes,
        char_width=char_width,
        flags=flags,
        bbox=bbox,
        ascent=ascent,
        descent=descent,
        cap_height=cap_height,
        italic_angle=italic_angle,
        stem_v=stem_v,
        cid_to_gid=cid_to_gid,
        widths=widths,
    )


def _missing_chars(source: str | bytes, face: int, text: str) -> list[str]:
    font = _load_font(source, face)
    if font is None:
        return list(dict.fromkeys(text))
    try:
        cmap = font.getBestCmap() or {}
    except Exception:
        return list(dict.fromkeys(text))
    finally:
        with contextlib.suppress(Exception):
            font.close()
    return [char for char in dict.fromkeys(text) if ord(char) not in cmap]


# --------------------------------------------------------------------------- #
# ToUnicode
# --------------------------------------------------------------------------- #
def _bfchar_blocks(pairs: list[tuple[bytes, str]]) -> str:
    unique: dict[bytes, str] = {}
    for code, char in pairs:
        unique.setdefault(code, char)
    items = list(unique.items())
    lines: list[str] = []
    for start in range(0, len(items), 100):
        chunk = items[start : start + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for code, char in chunk:
            target = char.encode("utf-16-be")
            lines.append(f"<{code.hex()}> <{target.hex()}>")
        lines.append("endbfchar")
    return "\n".join(lines)


def _tounicode_cmap(pairs: list[tuple[bytes, str]]) -> bytes:
    size = len(pairs[0][0]) if pairs else 1
    low = "00" * size
    high = "ff" * size
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        f"<{low}> <{high}>",
        "endcodespacerange",
    ]
    blocks = _bfchar_blocks(pairs)
    if blocks:
        lines.append(blocks)
    lines += [
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    return ("\n".join(lines) + "\n").encode("latin-1")


def _codespace_fits(cmap_text: str, pairs: list[tuple[bytes, str]]) -> bool:
    match = re.search(r"begincodespacerange(.*?)endcodespacerange", cmap_text, re.S)
    if match is None:
        return False
    ranges = re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", match.group(1))
    if not ranges:
        return False
    for code, _char in pairs:
        hexed = code.hex()
        fits = False
        for low, high in ranges:
            if (
                len(hexed) == len(low)
                and int(hexed, 16) >= int(low, 16)
                and int(hexed, 16) <= int(high, 16)
            ):
                fits = True
                break
        if not fits:
            return False
    return True


def _type0_full_pairs(
    font_dict: pikepdf.Dictionary,
    prog_bytes: bytes | None,
    kind: str,
    codes: dict[str, bytes],
) -> list[tuple[bytes, str]] | None:
    if kind != "ttf" or prog_bytes is None:
        return None
    desc = _descendant(font_dict)
    if desc is None:
        return None
    font = _load_font(prog_bytes, 0)
    if font is None:
        return None
    try:
        cmap = font.getBestCmap() or {}
        gid_to_cp: dict[int, int] = {}
        for cp, glyph_name in cmap.items():
            gid_to_cp.setdefault(font.getGlyphID(glyph_name), cp)
    except Exception:
        with contextlib.suppress(Exception):
            font.close()
        return None
    with contextlib.suppress(Exception):
        font.close()
    out: dict[bytes, str] = {}
    for char, code in codes.items():
        out[code] = char
    cid_to_gid = desc.get("/CIDToGIDMap")
    if isinstance(cid_to_gid, pikepdf.Stream):
        raw = cid_to_gid.read_bytes()
        count = len(raw) // 2
        id_pairs = struct.unpack(f">{count}H", raw[: count * 2])
        for cid, glyph_id in enumerate(id_pairs):
            cp = gid_to_cp.get(glyph_id)
            if cp is not None:
                out.setdefault(cid.to_bytes(2, "big"), chr(cp))
    else:
        for glyph_id, cp in gid_to_cp.items():
            out.setdefault(glyph_id.to_bytes(2, "big"), chr(cp))
    return list(out.items())


# --------------------------------------------------------------------------- #
# measurement and small math helpers
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=8)
def _mupdf_base14(name: str) -> pymupdf.Font | None:
    try:
        return pymupdf.Font(fontname=name)
    except Exception:
        return None


def _base14_key(family: str) -> str | None:
    lowered = family.lower()
    for prefix, name in _BASE14_PYMUPDF:
        if lowered.startswith(prefix):
            return name
    return None


def _width_of(
    text: str,
    char_width: dict[str, int] | None,
    base14: str | None,
    size: float,
    tc: float,
    tw: float,
    tz: float,
    scale: float,
) -> float:
    if not text:
        return 0.0
    base: float | None = None
    if char_width is not None and all(char in char_width for char in text):
        base = sum(char_width[char] for char in text) / 1000.0 * size
    elif base14 is not None:
        font = _mupdf_base14(base14)
        if font is not None:
            base = font.text_length(text, fontsize=size)
    if base is None:
        base = 0.55 * size * len(text)
    total = base + tc * len(text) + tw * text.count(" ")
    return total * (tz / 100.0) * scale


def _mat6(operands: list[Any]) -> Matrix | None:
    values: list[float] = []
    for item in operands:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        values.append(float(item))
    if len(values) != 6:
        return None
    return (values[0], values[1], values[2], values[3], values[4], values[5])


def _mat_inv(m: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        return IDENTITY
    inv_a, inv_b, inv_c, inv_d = d / det, -b / det, -c / det, a / det
    return (
        inv_a,
        inv_b,
        inv_c,
        inv_d,
        -(inv_a * e + inv_c * f),
        -(inv_b * e + inv_d * f),
    )


def _op(instruction: Any) -> str:
    operator = getattr(instruction, "operator", None)
    return str(operator) if operator is not None else ""


def _operands(instruction: Any) -> list[Any]:
    values = getattr(instruction, "operands", None)
    return list(values) if values else []


def _final_ctm(instructions: list[Any]) -> Matrix:
    ctm: Matrix = IDENTITY
    stack: list[Matrix] = []
    for instruction in instructions:
        operator = _op(instruction)
        if operator == "q":
            stack.append(ctm)
        elif operator == "Q":
            ctm = stack.pop() if stack else ctm
        elif operator == "cm":
            matrix = _mat6(_operands(instruction))
            if matrix is not None:
                ctm = mat_mul(matrix, ctm)
    return ctm


def _color_ops(fill: ColorValue) -> tuple[list[tuple[list[Any], str]], str]:
    space = fill.space
    components: list[Any] = list(fill.components)
    if space == "DeviceGray" and len(components) == 1:
        return [([components[0]], "g")], ""
    if space == "DeviceRGB" and len(components) == 3:
        return [(components, "rg")], ""
    if space == "DeviceCMYK" and len(components) == 4:
        return [(components, "k")], ""
    if space in ("DeviceGray", "DeviceRGB", "DeviceCMYK"):
        return [], "text colour could not be reproduced"
    return [], f"colour space {space} is not reproduced by the fallback writer"


# --------------------------------------------------------------------------- #
# plan objects
# --------------------------------------------------------------------------- #
@dataclass
class _ToUnicodePlan:
    mode: str  # "merge" | "fresh"
    pairs: list[tuple[bytes, str]] = field(default_factory=list)
    existing: pikepdf.Stream | None = None


@dataclass
class _Slot:
    key: str
    kind: str  # "reuse" | "embed"
    res_name: str | None
    char_bytes: dict[str, bytes]
    char_width: dict[str, int] | None
    from_original: bool
    embed: _Embedded | None = None
    tounicode: _ToUnicodePlan | None = None
    font_dict: pikepdf.Dictionary | None = None
    assigned: str | None = None

    @property
    def effective_name(self) -> str:
        return self.assigned or self.res_name or ""


@dataclass
class _Plan:
    page: pikepdf.Page
    page_index: int
    span_id: int
    old_text: str
    new_text: str
    decision: FontDecision
    runs: list[tuple[_Slot, str, bytes]]
    events: list[ShowEvent]
    stream_id: str | None
    fallback: bool
    warning: str
    original_width: float
    new_width: float
    overflow: bool
    reuse_pct: int
    noop: bool = False
    reflow: bool = False
    leading: float = 0.0
    frame: BlockFrame | None = None
    flow: FlowBlock | None = None
    collisions: list[tuple[str, tuple[float, float, float, float]]] = field(
        default_factory=list
    )
    pushed: float = 0.0
    add_color: ColorValue | None = None

    @property
    def path(self) -> str:
        return "fallback" if self.fallback else "in-place"

    def embed_slots(self) -> list[_Slot]:
        out: dict[str, _Slot] = {}
        for slot, _text, _data in self.runs:
            if slot.kind == "embed":
                out.setdefault(slot.key, slot)
        return list(out.values())


@dataclass
class EditResult:
    """Outcome of one edit request (also feeds the Edit-mode badge)."""

    ok: bool
    warning: str = ""
    decision: FontDecision | None = None
    path: str = "none"  # "in-place" | "fallback" | "none"
    badge: str = ""
    noop: bool = False
    overflow: bool = False
    original_width: float = 0.0
    new_width: float = 0.0
    old_text: str = ""
    new_text: str = ""
    duration_ms: float = 0.0
    collisions: list[tuple[str, tuple[float, float, float, float]]] = field(
        default_factory=list
    )
    pushed: float = 0.0
    lines_before: int = 0
    lines_after: int = 0
    scale: float = 1.0
    tracking: float = 0.0


class EditLineCommand(Command):
    """Undo/redo pair holding the whole document before and after an edit."""

    def __init__(
        self,
        description: str,
        before: bytes,
        after: bytes,
        notify: Callable[[], None],
    ) -> None:
        self.description = description
        self._before = before
        self._after = after
        self._notify = notify

    def redo(self, session: PdfSession) -> None:
        session.replace_bytes(zlib.decompress(self._after))
        self._notify()

    def undo(self, session: PdfSession) -> None:
        session.replace_bytes(zlib.decompress(self._before))
        self._notify()


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
class EditEngine:
    """Single-line in-place editing for one open document."""

    def __init__(
        self,
        session: PdfSession,
        inspect: InspectSession,
        resolver: FontResolver | None = None,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        self.session = session
        self.inspect = inspect
        self.resolver = resolver if resolver is not None else FontResolver()
        self._on_changed = on_changed
        self._column_cache: dict[int, tuple[float, float]] = {}

    # -- public API --------------------------------------------------------- #
    def can_edit(self, page_index: int, span_id: int) -> str:
        """Return "" when the span can be edited, else the refusal reason."""
        try:
            self._validate(page_index, span_id)
        except _Reject as reject:
            return reject.result.warning
        return ""

    def decision_for(
        self, page_index: int, span_id: int, text: str
    ) -> FontDecision | None:
        """Resolve the font decision for ``text`` without touching the file."""
        try:
            plan = self._build_plan(page_index, span_id, text)
        except _Reject as reject:
            return reject.result.decision
        except Exception:  # pragma: no cover - UI robustness
            logger.exception("decision preview failed")
            return None
        return plan.decision

    def preview(
        self, page_index: int, span_id: int, text: str
    ) -> tuple[FontDecision | None, str]:
        """Cheap live preview: resolver answer only (no plan, no embedding)."""
        try:
            model, _span, events = self._validate(page_index, span_id)
        except _Reject as reject:
            return reject.result.decision, reject.result.warning
        first = events[0]
        if first.font is None:  # pragma: no cover - checked in _validate
            return None, "The font resource for this text is missing."
        ref = first.font
        font_dict = model.font_dicts.get(ref.objgen)
        if font_dict is None:
            return None, "The font dictionary for this text could not be read."
        packed = program_bytes(font_dict)
        prog_bytes, kind = packed if packed else (None, "")
        program = self.inspect.programs.get(ref)
        request = FontRequest(
            text=_sanitize(text),
            program=program,
            font_bytes=prog_bytes,
            font_kind=kind,
            weight=program.weight,
            italic=program.italic,
            cache_key=f"{ref.objgen}|{_sanitize(text)}",
        )
        try:
            return self.resolver.resolve(request), ""
        except Exception:  # pragma: no cover - UI robustness
            logger.exception("preview resolve failed")
            return None, "The font decision could not be computed."

    def edit_line(self, page_index: int, span_id: int, text: str) -> EditResult:
        """Replace the text of ``span_id`` in place and record undo history."""
        started = time.perf_counter()
        text = _sanitize(text)
        try:
            plan = self._build_plan(page_index, span_id, text)
        except _Reject as reject:
            reject.result.duration_ms = (time.perf_counter() - started) * 1000.0
            return reject.result

        decision = plan.decision
        if plan.noop:
            return EditResult(
                ok=True,
                decision=decision,
                path="none",
                badge=_badge(plan),
                noop=True,
                original_width=plan.original_width,
                new_width=plan.new_width,
                old_text=plan.old_text,
                new_text=plan.new_text,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

        before = zlib.compress(self.session.to_bytes(), 6)
        try:
            self._commit(plan)
            self._post_commit(page_index)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("edit commit failed")
            with contextlib.suppress(Exception):
                self.session.replace_bytes(zlib.decompress(before))
            return EditResult(
                ok=False,
                warning=f"The edit could not be applied: {exc}",
                decision=decision,
                old_text=plan.old_text,
                new_text=plan.new_text,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

        self.session.refresh_view()
        self.session.mark_dirty()
        self._after_mutation()
        after = zlib.compress(self.session.to_bytes(), 6)
        self.session.history.record(
            EditLineCommand(
                f"Edit text on page {page_index + 1}",
                before,
                after,
                self._after_mutation,
            )
        )
        return EditResult(
            ok=True,
            warning=plan.warning,
            decision=decision,
            path=plan.path,
            badge=_badge(plan),
            overflow=plan.overflow,
            original_width=plan.original_width,
            new_width=plan.new_width,
            old_text=plan.old_text,
            new_text=plan.new_text,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )

    # -- validation --------------------------------------------------------- #
    def _validate(
        self, page_index: int, span_id: int
    ) -> tuple[PageModel, TextSpan, list[ShowEvent]]:
        if not (0 <= page_index < self.inspect.page_count):
            raise _Reject(_fail("That page no longer exists."))
        model = self.inspect.model(page_index)
        span = next((s for s in model.spans if s.id == span_id), None)
        if span is None:
            raise _Reject(_fail("That text is no longer available."))
        if not span.chars:
            raise _Reject(_fail("There is no text to edit here."))
        mapped = [char for char in span.chars if char.event_idx >= 0]
        if not mapped:
            raise _Reject(
                _fail(
                    "This text is not drawn by a text-showing operation "
                    "(outlined or imported as graphics) - editing is disabled."
                )
            )
        if len(mapped) != len(span.chars):
            self._attribute_unmapped(model, span)
            mapped = [char for char in span.chars if char.event_idx >= 0]
            if len(mapped) != len(span.chars):
                raise _Reject(
                    _fail(
                        "Part of this text could not be located in the content "
                        "stream, so the line was left unchanged."
                    )
                )

        owner: dict[int, int] = {}
        for char, other in model.chars:
            if char.event_idx < 0:
                continue
            previous = owner.setdefault(char.event_idx, other.id)
            if previous != other.id:
                owner[char.event_idx] = -1

        events: list[ShowEvent] = []
        for index in sorted({char.event_idx for char in span.chars}):
            if owner.get(index, -1) != span.id:
                raise _Reject(
                    _fail(
                        "This text shares a drawing operation with neighbouring "
                        "text, so it cannot be replaced safely."
                    )
                )
            event = model.shows[index]
            if event.font is None:
                raise _Reject(
                    _fail("The font resource for this text could not be resolved.")
                )
            if event.font.subtype == "Type3":
                raise _Reject(
                    _fail(
                        "Type3 (drawn) fonts cannot be edited safely - "
                        "the text was left unchanged."
                    )
                )
            if not event.in_text_object:
                raise _Reject(
                    _fail("This text sits outside a BT/ET block and cannot be edited.")
                )
            events.append(event)
        return model, span, events

    def _attribute_unmapped(self, model: PageModel, span: TextSpan) -> None:
        """Attribute unmapped chars to their sole owning show event.

        MuPDF text extraction reorders right-to-left runs into logical order,
        so for a pure RTL line only the leftmost char anchors to the event and
        the rest stay unmapped.  When exactly one event draws exactly this
        span's glyphs, every char belongs to it regardless of order.
        """
        owners = {char.event_idx for char in span.chars if char.event_idx >= 0}
        message = (
            "Part of this text could not be located in the content "
            "stream, so the line was left unchanged."
        )
        if len(owners) != 1:
            raise _Reject(_fail(message))
        index = next(iter(owners))
        event = model.shows[index]
        if event.glyph_count != len(span.chars):
            raise _Reject(_fail(message))
        for char in span.chars:
            if char.event_idx < 0:
                char.event_idx = index

    # -- planning ----------------------------------------------------------- #
    def _build_plan(self, page_index: int, span_id: int, text: str) -> _Plan:
        model, span, events = self._validate(page_index, span_id)
        old_text = span.text
        stream_text = _visual_rtl(text)
        first = events[0]
        if first.font is None:  # pragma: no cover - checked in _validate
            raise _Reject(_fail("The font resource for this text is missing."))
        ref = first.font

        warning = ""
        if self.session.is_signed:
            warning += (
                "This document is digitally signed; editing invalidates the "
                "signature. "
            )
        if self.session.is_encrypted:
            warning += "The document is encrypted; the saved copy stays encrypted. "

        font_dict = model.font_dicts.get(ref.objgen)
        if font_dict is None:
            raise _Reject(
                _fail("The font dictionary for this text could not be read.")
            )
        packed = program_bytes(font_dict)
        prog_bytes, kind = packed if packed else (None, "")
        program = self.inspect.programs.get(ref)

        request = FontRequest(
            text=text,
            program=program,
            font_bytes=prog_bytes,
            font_kind=kind,
            weight=program.weight,
            italic=program.italic,
            cache_key=f"{ref.objgen}|{text}",
        )
        decision = self.resolver.resolve(request)

        slot: _Slot | None = None
        runs: list[tuple[_Slot, str, bytes]] = []
        if decision.strategy == STRATEGY_REUSE:
            encoded = _encode_original(stream_text, ref, font_dict, prog_bytes, kind)
            if encoded is not None:
                data, codes, table = encoded
                tounicode = self._reuse_tounicode(
                    ref, font_dict, prog_bytes, kind, codes, table
                )
                if tounicode is not None:
                    slot = _Slot(
                        key=f"reuse:{ref.objgen}",
                        kind="reuse",
                        res_name=ref.res_name,
                        char_bytes=codes,
                        char_width=_pdf_char_widths(font_dict, codes)
                        or self._reuse_widths(prog_bytes, kind, text),
                        from_original=True,
                        tounicode=tounicode,
                        font_dict=font_dict,
                    )
                    runs = [(slot, text, data)]
            if slot is None:
                embedded = _embed_program(prog_bytes, 0, stream_text) if prog_bytes else None
                if embedded is not None:
                    slot = self._slot_from_embedded(embedded, from_original=True)
                    runs = [(slot, text, _encode_run(slot, stream_text))]
                    warning += (
                        "the original font could not be re-encoded, so a subset "
                        "of it was embedded; "
                    )
                else:
                    decision = self.resolver.resolve(
                        _dc_replace(
                            request,
                            program=_dc_replace(program, coverage=frozenset()),
                            cache_key=request.cache_key + "#sub",
                        )
                    )
                    if decision.strategy == STRATEGY_REUSE or decision.path is None:
                        raise _Reject(
                            _fail(
                                "No offline font can encode this text - "
                                "the line was left unchanged.",
                                decision=decision,
                            )
                        )
                    warning += (
                        "the original font cannot encode the text, so a "
                        "substitute was used; "
                    )
        if slot is None:
            built, error = self._embed_runs(decision, stream_text)
            if built is None:
                raise _Reject(_fail(error or "The font could not be prepared."))
            runs = [
                (chosen, chunk, _encode_run(chosen, chunk)) for chosen, chunk in built
            ]

        stream_ids = {event.stream_id for event in events}
        fallback = len(stream_ids) > 1
        stream_id = None if fallback else next(iter(stream_ids))

        size = first.tf_size
        scale = mat_scale(first.tm)
        original_width = self._original_width(
            model, span, program, prog_bytes, kind, old_text, first
        )
        new_width = sum(
            _width_of(
                chunk,
                chosen.char_width,
                _base14_key(program.family) if chosen.kind == "reuse" else None,
                size,
                first.tc,
                first.tw,
                first.tz,
                scale,
            )
            for chosen, chunk, _data in runs
        )

        reuse_chars = sum(
            len(chunk) for chosen, chunk, _data in runs if chosen.from_original
        )
        reuse_pct = round(100 * reuse_chars / len(text)) if text else 100

        try:
            page = self.session.pdf.pages[page_index]
        except Exception as exc:  # pragma: no cover - defensive
            raise _Reject(_fail(f"Page could not be opened: {exc}")) from exc

        plan = _Plan(
            page=page,
            page_index=page_index,
            span_id=span_id,
            old_text=old_text,
            new_text=text,
            decision=decision,
            runs=runs,
            events=events,
            stream_id=stream_id,
            fallback=fallback,
            warning=warning,
            original_width=original_width,
            new_width=new_width,
            overflow=new_width > original_width + 1.0,
            reuse_pct=reuse_pct,
        )
        if text == old_text and len(runs) == 1 and runs[0][0].kind == "reuse":
            plan.noop = True
        return plan

    def _reuse_tounicode(
        self,
        ref: FontRef,
        font_dict: pikepdf.Dictionary,
        prog_bytes: bytes | None,
        kind: str,
        codes: dict[str, bytes],
        table: dict[int, int],
    ) -> _ToUnicodePlan | None:
        our_pairs = [(encoded, char) for char, encoded in codes.items()]
        existing = font_dict.get("/ToUnicode")
        if isinstance(existing, pikepdf.Stream):
            body = existing.read_bytes().decode("latin-1", "replace")
            if not _codespace_fits(body, our_pairs):
                return None
            return _ToUnicodePlan(mode="merge", pairs=our_pairs, existing=existing)
        if ref.subtype == "Type0":
            pairs = _type0_full_pairs(font_dict, prog_bytes, kind, codes)
            if pairs is None:
                return None
            return _ToUnicodePlan(mode="fresh", pairs=pairs)
        merged: dict[bytes, str] = {}
        for encoded, char in our_pairs:
            merged[encoded] = char
        for cp, codepoint in table.items():
            merged.setdefault(bytes((codepoint,)), chr(cp))
        return _ToUnicodePlan(mode="fresh", pairs=list(merged.items()))

    def _reuse_widths(
        self, prog_bytes: bytes | None, kind: str, text: str
    ) -> dict[str, int] | None:
        if not prog_bytes or not kind:
            return None
        widths = advance_widths(prog_bytes, kind, [ord(char) for char in text])
        if not widths:
            return None
        return {
            char: widths[ord(char)]
            for char in dict.fromkeys(text)
            if ord(char) in widths
        }

    def _slot_from_embedded(
        self, embedded: _Embedded, *, from_original: bool
    ) -> _Slot:
        return _Slot(
            key=f"embed:{id(embedded)}",
            kind="embed",
            res_name=None,
            char_bytes=embedded.char_bytes,
            char_width=embedded.char_width,
            from_original=from_original,
            embed=embedded,
            tounicode=_ToUnicodePlan(
                mode="fresh",
                pairs=[
                    (encoded, char) for char, encoded in embedded.char_bytes.items()
                ],
            ),
        )

    def _embed_runs(
        self, decision: FontDecision, text: str
    ) -> tuple[list[tuple[_Slot, str]] | None, str | None]:
        sources: list[tuple[str, int, str]] = []
        if decision.runs:
            for run in decision.runs:
                path = run.path if run.path is not None else decision.path
                face = run.face if run.path is not None else decision.face
                if path is None:
                    return None, (
                        "no offline font is available for this text - "
                        "the line was left unchanged."
                    )
                sources.append((path, face, run.text))
        else:
            if decision.path is None:
                return None, (
                    "no offline font is available for this text - "
                    "the line was left unchanged."
                )
            sources.append((decision.path, decision.face, text))

        order: list[tuple[str, int]] = []
        chunks: dict[tuple[str, int], list[str]] = {}
        for path, face, chunk in sources:
            key = (path, face)
            if key not in chunks:
                order.append(key)
                chunks[key] = []
            chunks[key].append(chunk)

        slots: dict[tuple[str, int], _Slot] = {}
        for key in order:
            path, face = key
            full = "".join(chunks[key])
            embedded = _embed_program(path, face, full)
            if embedded is None:
                missing = _missing_chars(path, face, full)
                if missing:
                    sample = ", ".join(repr(char) for char in missing[:6])
                    more = "" if len(missing) <= 6 else f" (+{len(missing) - 6} more)"
                    return None, (
                        f"the chosen font lacks glyph(s) for {sample}{more} - "
                        "the line was left unchanged."
                    )
                return None, (
                    "the chosen font could not be embedded - "
                    "the line was left unchanged."
                )
            slots[key] = self._slot_from_embedded(embedded, from_original=False)
        runs = [(slots[(path, face)], chunk) for path, face, chunk in sources]
        return runs, None

    def _original_width(
        self,
        model: PageModel,
        span: TextSpan,
        program: FontProgram,
        prog_bytes: bytes | None,
        kind: str,
        text: str,
        first: ShowEvent,
    ) -> float:
        scale = mat_scale(first.tm)
        char_width: dict[str, int] | None = None
        if prog_bytes and kind:
            widths = advance_widths(prog_bytes, kind, [ord(char) for char in text])
            if widths:
                char_width = {
                    char: widths[ord(char)]
                    for char in dict.fromkeys(text)
                    if ord(char) in widths
                }
        base14 = _base14_key(program.family) if not program.embedded else None
        if char_width is None and base14 is None:
            return max(span.bbox[2] - span.bbox[0], 0.0)
        return _width_of(
            text,
            char_width,
            base14,
            first.tf_size,
            first.tc,
            first.tw,
            first.tz,
            scale,
        )

    # -- commit ------------------------------------------------------------- #
    def _commit(self, plan: _Plan) -> None:
        pdf = self.session.pdf
        targets = self._targets(plan)
        embed_slots = plan.embed_slots()
        if embed_slots:
            self._assign_names(plan, embed_slots)
        if plan.fallback:
            prepared = self._prepare_fallback(plan, targets)
        else:
            prepared = [self._prepare_inplace(plan, targets)]
        if embed_slots:
            fonts = self._ensure_fonts(plan)
            for slot in embed_slots:
                if slot.embed is None or slot.tounicode is None:
                    continue  # pragma: no cover - slots always carry both
                fonts[pikepdf.Name("/" + slot.effective_name)] = _font_object(
                    pdf, slot.embed, slot.tounicode
                )
        for slot, _chunk, _data in plan.runs:
            if slot.kind == "reuse":
                self._apply_reuse_tounicode(slot)
        for target, data in prepared:
            if target is plan.page:
                plan.page.Contents = pdf.make_stream(data)
            else:
                target.write(data)

    def _targets(self, plan: _Plan) -> dict[str | None, Any]:
        pdf = self.session.pdf
        out: dict[str | None, Any] = {None: plan.page}
        for event in plan.events:
            if event.stream_id is None or event.stream_id in out:
                continue
            objnum, gennum = (int(part) for part in event.stream_id.split())
            out[event.stream_id] = pdf.get_object(objnum, gennum)
        return out

    def _assign_names(self, plan: _Plan, embed_slots: list[_Slot]) -> None:
        resources = self._ensure_resources(plan, plan.stream_id)
        used: set[str] = set()
        fonts = resources.get("/Font")
        if isinstance(fonts, pikepdf.Dictionary):
            used = {str(key).lstrip("/") for key in fonts}
        counter = 1
        for slot in embed_slots:
            while f"{_FONT_PREFIX}{counter}" in used:
                counter += 1
            name = f"{_FONT_PREFIX}{counter}"
            used.add(name)
            slot.assigned = name

    def _ensure_resources(self, plan: _Plan, stream_id: str | None) -> Any:
        if stream_id is None:
            resources = plan.page.get("/Resources")
            if not isinstance(resources, pikepdf.Dictionary):
                resources = pikepdf.Dictionary()
                plan.page.Resources = resources
            return resources
        objnum, gennum = (int(part) for part in stream_id.split())
        form = self.session.pdf.get_object(objnum, gennum)
        resources = form.get("/Resources")
        if isinstance(resources, pikepdf.Dictionary):
            return resources
        return self._ensure_resources(plan, None)

    def _ensure_fonts(self, plan: _Plan) -> Any:
        resources = self._ensure_resources(
            plan, None if plan.fallback else plan.stream_id
        )
        fonts = resources.get("/Font")
        if not isinstance(fonts, pikepdf.Dictionary):
            fonts = pikepdf.Dictionary()
            resources.Font = fonts
        return fonts

    def _apply_reuse_tounicode(self, slot: _Slot) -> None:
        tounicode = slot.tounicode
        if tounicode is None or slot.font_dict is None:
            return
        if tounicode.mode == "merge" and tounicode.existing is not None:
            body = tounicode.existing.read_bytes().decode("latin-1", "replace")
            index = body.rfind("endcmap")
            if index < 0:
                return
            blocks = _bfchar_blocks(tounicode.pairs)
            if not blocks:
                return
            merged = body[:index] + blocks + "\n" + body[index:]
            tounicode.existing.write(merged.encode("latin-1"))
        elif tounicode.mode == "fresh":
            slot.font_dict.ToUnicode = self.session.pdf.make_stream(
                _tounicode_cmap(tounicode.pairs)
            )

    # -- instruction rewriting --------------------------------------------- #
    def _prepare_inplace(
        self, plan: _Plan, targets: dict[str | None, Any]
    ) -> tuple[Any, bytes]:
        target = targets[plan.stream_id]
        instructions = list(pikepdf.parse_content_stream(target))
        for event in plan.events:
            if not (0 <= event.instr < len(instructions)):
                raise _PlanError("the content stream changed")
            if _op(instructions[event.instr]) not in _SHOW_OPS:
                raise _PlanError("the content stream changed")

        first = plan.events[0]
        size = first.tf_size
        state_name = first.font.res_name if first.font else ""
        others = {event.instr for event in plan.events[1:]}
        out: list[Any] = []
        switched = False
        for index, instruction in enumerate(instructions):
            operator = _op(instruction)
            if index == first.instr:
                slot0, _text, data0 = plan.runs[0]
                if slot0.effective_name != state_name:
                    out.append(([pikepdf.Name("/" + slot0.effective_name), size], "Tf"))
                    switched = True
                out.append(
                    (_show_operands(_operands(instruction), operator, data0), operator)
                )
                for slot, _chunk, data in plan.runs[1:]:
                    out.append(([pikepdf.Name("/" + slot.effective_name), size], "Tf"))
                    out.append(([pikepdf.String(data)], "Tj"))
                    switched = True
                if switched:
                    out.append(([pikepdf.Name("/" + state_name), size], "Tf"))
            elif index in others:
                out.append(
                    (_blank_operands(_operands(instruction), operator), operator)
                )
            else:
                out.append(instruction)
        return target, pikepdf.unparse_content_stream(out)

    def _prepare_fallback(
        self, plan: _Plan, targets: dict[str | None, Any]
    ) -> list[tuple[Any, bytes]]:
        groups: dict[str | None, list[ShowEvent]] = {}
        for event in plan.events:
            groups.setdefault(event.stream_id, []).append(event)

        prepared: list[tuple[Any, bytes]] = []
        page_instructions: list[Any] | None = None
        for stream_id, events in groups.items():
            target = targets[stream_id]
            instructions = list(pikepdf.parse_content_stream(target))
            for event in events:
                if not (0 <= event.instr < len(instructions)):
                    raise _PlanError("the content stream changed")
                if _op(instructions[event.instr]) not in _SHOW_OPS:
                    raise _PlanError("the content stream changed")
            wanted = {event.instr for event in events}
            out: list[Any] = []
            for index, instruction in enumerate(instructions):
                if index in wanted:
                    out.append(
                        (
                            _blank_operands(_operands(instruction), _op(instruction)),
                            _op(instruction),
                        )
                    )
                else:
                    out.append(instruction)
            if stream_id is None:
                page_instructions = out
            else:
                prepared.append((target, pikepdf.unparse_content_stream(out)))

        if page_instructions is None:
            try:
                page_instructions = list(pikepdf.parse_content_stream(plan.page))
            except Exception:
                page_instructions = []
        block, block_warning = self._fallback_block(
            plan, _final_ctm(page_instructions)
        )
        if block_warning:
            plan.warning += block_warning + "; "
        prepared.append(
            (plan.page, pikepdf.unparse_content_stream(page_instructions + block))
        )
        return prepared

    def _fallback_block(
        self, plan: _Plan, final_ctm: Matrix
    ) -> tuple[list[tuple[list[Any], str]], str]:
        first = plan.events[0]
        ops: list[tuple[list[Any], str]] = []
        warning = ""
        ops.append(([], "q"))
        matrix = mat_mul(first.ctm, _mat_inv(final_ctm))
        ops.append((list(matrix), "cm"))
        color_ops, color_warning = _color_ops(first.fill)
        ops.extend(color_ops)
        if color_warning:
            warning = color_warning
        if first.fill_alpha < 1.0:
            warning = warning or "text transparency is not reproduced"
        ops.append(([], "BT"))
        ops.append((list(first.tm), "Tm"))
        slot0, _text, data0 = plan.runs[0]
        ops.append(([pikepdf.Name("/" + slot0.effective_name), first.tf_size], "Tf"))
        if first.tc:
            ops.append(([first.tc], "Tc"))
        if first.tw:
            ops.append(([first.tw], "Tw"))
        if first.tz != 100.0:
            ops.append(([first.tz], "Tz"))
        if first.ts:
            ops.append(([first.ts], "Ts"))
        ops.append(([pikepdf.String(data0)], "Tj"))
        for slot, _chunk, data in plan.runs[1:]:
            ops.append(([pikepdf.Name("/" + slot.effective_name), first.tf_size], "Tf"))
            ops.append(([pikepdf.String(data)], "Tj"))
        ops.append(([], "ET"))
        ops.append(([], "Q"))
        return ops, warning

    # -- paragraph reflow --------------------------------------------------- #
    def paragraph_for_line_edit(
        self, page_index: int, span_id: int, text: str
    ) -> tuple[int, str] | None:
        """``(block id, paragraph text)`` when a line edit should reflow instead.

        That is when the edited line would run past its paragraph's column
        and the paragraph is a single style run over several lines (reflow
        rebuilds the paragraph in one style, so mixed-style paragraphs keep
        the per-run line edit).  ``None`` means: edit the line in place.
        """
        try:
            model = self.inspect.model(page_index)
        except Exception:
            return None
        found = None
        for block_id, block in enumerate(model.blocks):
            for line in block.lines:
                for span in line.spans:
                    if span.id == span_id:
                        found = (block_id, block, line, span)
        if found is None:
            return None
        block_id, block, line, span = found
        if len(block.lines) < 2:
            return None
        styled = [s for ln in block.lines for s in ln.spans if s.chars]
        first = styled[0]
        fonts = {
            model.shows[s.chars[0].event_idx].font.objgen  # type: ignore[union-attr]
            for s in styled
            if s.chars[0].event_idx >= 0 and model.shows[s.chars[0].event_idx].font
        }
        single_style = len(fonts) <= 1 and all(
            abs(s.size_mupdf - first.size_mupdf) < 0.1 and s.color_int == first.color_int
            for s in styled
        )
        del single_style  # mixed styles are kept per run by reflow_line
        if self.can_reflow(page_index, block_id):
            return None
        try:
            plan = self._build_plan(page_index, span_id, _sanitize(text))
            _model, blk, events = self._block_events(page_index, block_id)
            _frame, available, _indent, _hanging = self._reflow_geometry(model, blk, events)
        except _Reject:
            return None
        except Exception:  # pragma: no cover - UI robustness
            logger.exception("paragraph check failed")
            return None
        horizontal = abs(line.dir[0]) >= abs(line.dir[1])
        x0, y0, x1, y1 = line.bbox
        line_width = (x1 - x0) if horizontal else (y1 - y0)
        new_line_width = line_width - plan.original_width + plan.new_width
        if new_line_width <= available + 1.0:
            return None
        parts = []
        for ln in block.lines:
            if ln is line:
                parts.append("".join(text if s.id == span_id else s.text for s in ln.spans))
            else:
                parts.append(ln.text)
        paragraph = " ".join(part.strip() for part in parts if part.strip())
        return block_id, paragraph

    def can_reflow(self, page_index: int, block_id: int) -> str:
        """Return "" when the block can be reflowed, else the refusal reason."""
        try:
            self._block_events(page_index, block_id)
        except _Reject as reject:
            return reject.result.warning
        return ""

    def edit_block(
        self,
        page_index: int,
        block_id: int,
        text: str,
        *,
        overflow_mode: str = "push_down",
        leading: float | None = None,
        accept_overlap: bool = False,
    ) -> EditResult:
        """Re-wrap ``text`` into ``block_id``'s column and apply the edits.

        The font size and horizontal scale are kept: longer text wraps onto
        more lines (``push_down`` moves following content out of the way).
        Only ``overflow_mode="auto_fit"`` may narrow the text to fit.
        """
        started = time.perf_counter()
        text = _sanitize(text)
        if any(_is_rtl(char) for char in text):
            reject = _fail("Right-to-left paragraph reflow is not supported yet.")
            reject.duration_ms = (time.perf_counter() - started) * 1000.0
            return reject
        try:
            model, block, events = self._block_events(page_index, block_id)
        except _Reject as reject:
            reject.result.duration_ms = (time.perf_counter() - started) * 1000.0
            return reject.result
        anchor = next(
            (span for line in block.lines for span in line.spans if span.chars),  # type: ignore[attr-defined]
            None,
        )
        if anchor is None:
            return _fail("The block has no text to reflow.")
        try:
            plan = self._build_plan(page_index, anchor.id, text)
        except _Reject as reject:
            reject.result.duration_ms = (time.perf_counter() - started) * 1000.0
            return reject.result

        old_text = "".join(line.text for line in block.lines)  # type: ignore[attr-defined]
        first = events[0]
        return self._finish_reflow(
            page_index,
            model,
            block_id,
            block,
            events,
            plan,
            self._flow_runs_for(plan, first.tf_size),
            old_text,
            text,
            overflow_mode=overflow_mode,
            leading=leading,
            accept_overlap=accept_overlap,
            started=started,
        )

    def reflow_line(
        self,
        page_index: int,
        span_id: int,
        text: str,
        *,
        overflow_mode: str = "push_down",
    ) -> EditResult:
        """Replace one span's text and re-wrap its paragraph, keeping every run's style.

        Unlike :meth:`edit_block`, each original span keeps its own font,
        size and colour (a bold or coloured word stays so); only the edited
        span's text changes.  Size and horizontal scale are never reduced
        unless ``overflow_mode="auto_fit"``.
        """
        started = time.perf_counter()
        text = _sanitize(text)
        if any(_is_rtl(char) for char in text):
            reject = _fail("Right-to-left paragraph reflow is not supported yet.")
            reject.duration_ms = (time.perf_counter() - started) * 1000.0
            return reject
        try:
            model = self.inspect.model(page_index)
        except Exception as exc:  # pragma: no cover - defensive
            return _fail(f"The page could not be read: {exc}")
        block_id = next(
            (
                index
                for index, blk in enumerate(model.blocks)
                for ln in blk.lines
                for sp in ln.spans
                if sp.id == span_id
            ),
            None,
        )
        if block_id is None:
            return _fail("That text is no longer available.")
        try:
            model, block, events = self._block_events(page_index, block_id)
        except _Reject as reject:
            reject.result.duration_ms = (time.perf_counter() - started) * 1000.0
            return reject.result
        lines = [ln for ln in block.lines if any(sp.chars for sp in ln.spans)]  # type: ignore[attr-defined]
        pieces: list[tuple[TextSpan, str]] = []
        for line_no, ln in enumerate(lines):
            spans = [sp for sp in ln.spans if sp.chars]
            for index, sp in enumerate(spans):
                piece = text if sp.id == span_id else sp.text
                last_of_line = index == len(spans) - 1 and line_no < len(lines) - 1
                if last_of_line and not piece.endswith(" "):
                    piece += " "  # the line break becomes a space in the paragraph
                pieces.append((sp, piece))
        plans: list[_Plan] = []
        try:
            for sp, piece in pieces:
                plans.append(self._build_plan(page_index, sp.id, piece))
        except _Reject as reject:
            reject.result.duration_ms = (time.perf_counter() - started) * 1000.0
            return reject.result
        anchor = next(
            (pl for (sp, _piece), pl in zip(pieces, plans, strict=True) if sp.id == span_id),
            plans[0],
        )
        # Runs reusing the same original font share one slot (their code
        # tables are merged); a font embedded per run keeps its own resource,
        # since each embedded subset only holds that run's glyphs.
        by_key: dict[str, _Slot] = {}
        runs: list[tuple[_Slot, str, bytes]] = []
        flow_runs: list[FlowRun] = []
        for number, pl in enumerate(plans):
            for slot, _chunk, _data in pl.runs:
                if slot.kind != "reuse":
                    slot.key = f"{slot.key}#run{number}"
                    continue
                shared = by_key.setdefault(slot.key, slot)
                if shared is not slot:
                    shared.char_bytes.update(slot.char_bytes)
                    if shared.char_width is not None and slot.char_width:
                        shared.char_width.update(slot.char_width)
                    if shared.tounicode is not None and slot.tounicode is not None:
                        known = {code for code, _char in shared.tounicode.pairs}
                        shared.tounicode.pairs.extend(
                            pair for pair in slot.tounicode.pairs if pair[0] not in known
                        )
        for pl in plans:
            event = pl.events[0]
            base14 = None
            if pl.runs and pl.runs[0][0].kind == "reuse" and event.font is not None:
                base14 = _base14_key(self.inspect.programs.get(event.font).family)
            for slot, chunk, data in pl.runs:
                slot = by_key.setdefault(slot.key, slot)
                runs.append((slot, chunk, data))
                flow_runs.append(
                    FlowRun(
                        style=RunStyle(
                            size=event.tf_size,
                            char_width=slot.char_width,
                            base14=base14,
                            tc=event.tc,
                            tw=0.0,
                            tz=event.tz,
                            ts=event.ts,
                            fill=event.fill,
                        ),
                        text=chunk,
                        owner=slot,
                    )
                )
        anchor.runs = runs
        old_text = "".join(ln.text for ln in block.lines)  # type: ignore[attr-defined]
        new_text = "".join(piece for _sp, piece in pieces)
        return self._finish_reflow(
            page_index,
            model,
            block_id,
            block,
            events,
            anchor,
            flow_runs,
            old_text,
            new_text,
            overflow_mode=overflow_mode,
            leading=None,
            accept_overlap=False,
            started=started,
        )

    def _finish_reflow(
        self,
        page_index: int,
        model: PageModel,
        block_id: int,
        block: object,
        events: list[ShowEvent],
        plan: _Plan,
        flow_runs: list[FlowRun],
        old_text: str,
        text: str,
        *,
        overflow_mode: str,
        leading: float | None,
        accept_overlap: bool,
        started: float,
    ) -> EditResult:
        """Wrap ``flow_runs`` into the block's column and commit (shared by reflows)."""
        plan.span_id = block_id
        plan.events = events
        plan.stream_id = next(iter({event.stream_id for event in events}))
        plan.overflow = False
        plan.noop = text == old_text and plan.reuse_pct == 100

        frame, available, first_indent, hanging = self._reflow_geometry(
            model, block, events
        )
        lead = leading if leading and leading > 0 else self._block_leading(
            model, block, plan.events[0].tf_size, frame.axis, frame.down
        )
        first = plan.events[0]
        lines, too_wide = wrap_paragraph(
            flow_runs,
            available,
            first_indent=first_indent,
            hanging=hanging,
        )
        flow = FlowBlock(lines=lines)
        align_lines(lines, available, frame.alignment)
        mode = OverflowMode.parse(overflow_mode)
        if mode is OverflowMode.AUTO_FIT:
            apply_auto_fit(flow, available, size=first.tf_size)

        flow_bbox = flow_bbox_model(
            frame, flow, lead, size=max(first.tf_size, 1.0)
        )
        collisions = self._collisions(model, block, flow_bbox)
        plan.frame = frame
        plan.flow = flow
        plan.leading = lead
        plan.collisions = collisions
        plan.original_width = available if block.lines else 0.0  # type: ignore[attr-defined]
        plan.new_width = flow.rendered_widest()
        plan.overflow = flow.overflow or too_wide

        extras: list[_Plan] = []
        if collisions and not accept_overlap and mode is OverflowMode.PUSH_DOWN:
            extras = self._push_down(plan, model, block, events)
        if plan.noop:
            return EditResult(
                ok=True,
                decision=plan.decision,
                path="none",
                badge=_badge(plan),
                noop=True,
                old_text=old_text,
                new_text=text,
                original_width=plan.original_width,
                new_width=plan.new_width,
                overflow=plan.overflow,
                collisions=plan.collisions,
                lines_before=len(block.lines),  # type: ignore[attr-defined]
                lines_after=len(flow.lines),
                scale=flow.scale,
                tracking=flow.tracking,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

        before = zlib.compress(self.session.to_bytes(), 6)
        try:
            self._commit_reflow([plan, *extras])
            self._post_commit(page_index)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("reflow commit failed")
            with contextlib.suppress(Exception):
                self.session.replace_bytes(zlib.decompress(before))
            return EditResult(
                ok=False,
                warning=f"The reflow could not be applied: {exc}",
                decision=plan.decision,
                old_text=old_text,
                new_text=text,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

        self.session.refresh_view()
        self.session.mark_dirty()
        self._after_mutation()
        after = zlib.compress(self.session.to_bytes(), 6)
        self.session.history.record(
            EditLineCommand(
                f"Reflow text on page {page_index + 1}",
                before,
                after,
                self._after_mutation,
            )
        )
        warnings = []
        if plan.overflow:
            warnings.append("The paragraph still overflows its block width.")
        if collisions and not accept_overlap:
            warnings.append(
                f"The reflowed text overlaps {len(collisions)} element(s) "
                "on this page."
            )
        for extra in extras:
            if extra.warning:
                warnings.append(extra.warning)
        return EditResult(
            ok=True,
            warning=" ".join(warnings),
            decision=plan.decision,
            path="fallback",
            badge=_badge(plan),
            overflow=plan.overflow,
            original_width=plan.original_width,
            new_width=plan.new_width,
            old_text=old_text,
            new_text=text,
            collisions=plan.collisions,
            pushed=plan.pushed,
            lines_before=len(block.lines),  # type: ignore[attr-defined]
            lines_after=len(flow.lines),
            scale=flow.scale,
            tracking=flow.tracking,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )

    def add_text(
        self,
        page_index: int,
        x: float,
        y: float,
        text: str,
        *,
        width: float | None = None,
        size: float | None = None,
        color: ColorValue | None = None,
        alignment: str = "left",
        leading: float | None = None,
        preset: str | None = None,
        font_family: str | None = None,
        weight: int | None = None,
        italic: bool | None = None,
    ) -> EditResult:
        """Append a new text block at model-space ``(x, y)``."""
        started = time.perf_counter()
        text = _sanitize(text)
        if not text or not text.strip():
            return EditResult(
                ok=True, noop=True, duration_ms=(time.perf_counter() - started) * 1000.0
            )
        try:
            model = self.inspect.model(page_index)
        except Exception as exc:  # pragma: no cover - defensive
            return _fail(f"The page could not be read: {exc}")
        page = self.session.pdf.pages[page_index]
        top = _page_top(page)

        size, family, weight, italic, color = self._style_for_add(
            model, x, y, size, color, preset, font_family, weight, italic
        )
        if size <= 0:
            size = 10.5
        available = width or (model.mediabox[2] - model.mediabox[0] - x - 20.0)
        available = max(available, 20.0)

        program = FontProgram(
            objgen="",
            base_font=family,
            family=family,
            style="",
            weight=weight if weight is not None else 400,
            italic=bool(italic),
            monospace=False,
            embedded=False,
            source="default",
            coverage=None,
        )
        request = FontRequest(
            text=text,
            program=program,
            font_bytes=None,
            font_kind="",
            weight=weight,
            italic=italic,
            cache_key=f"addtext|{family}|{weight}|{italic}|{text}",
        )
        decision = self.resolver.resolve(request)
        if decision.strategy == STRATEGY_REUSE:
            return _fail("add_text cannot reuse the original font.")
        built, error = self._embed_runs(decision, text)
        if built is None:
            return _fail(error or "The font could not be prepared.")
        flow_runs = [
            FlowRun(
                RunStyle(size=size, char_width=slot.char_width, tc=0.0, tw=0.0),
                text_,
                owner=slot,
            )
            for slot, text_ in built
        ]
        lines, overflow = wrap_paragraph(flow_runs, available)
        flow = FlowBlock(lines=lines)
        align_lines(lines, available, Alignment(alignment) if alignment else Alignment.LEFT)
        lead = leading if leading and leading > 0 else size * 1.4
        frame = BlockFrame(
            origin=(x, top - y),
            axis=(1.0, 0.0),
            down=(0.0, -1.0),
            alignment=Alignment(alignment) if alignment else Alignment.LEFT,
        )
        plan = _Plan(
            page=page,
            page_index=page_index,
            span_id=-1,
            old_text="",
            new_text=text,
            decision=decision,
            runs=[(slot, text_, _encode_run(slot, text_)) for slot, text_ in built],
            events=[],
            stream_id=None,
            fallback=True,
            warning="",
            original_width=0.0,
            new_width=flow.rendered_widest(),
            overflow=overflow,
            reuse_pct=100,
            noop=False,
            reflow=True,
            leading=lead,
            frame=frame,
            flow=flow,
            add_color=color,
        )

        before = zlib.compress(self.session.to_bytes(), 6)
        try:
            self._commit_reflow([plan])
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("add_text commit failed")
            with contextlib.suppress(Exception):
                self.session.replace_bytes(zlib.decompress(before))
            return EditResult(
                ok=False, warning=f"The text could not be added: {exc}"
            )
        self.session.refresh_view()
        self.session.mark_dirty()
        self._after_mutation()
        after = zlib.compress(self.session.to_bytes(), 6)
        self.session.history.record(
            EditLineCommand(
                f"Add text on page {page_index + 1}",
                before,
                after,
                self._after_mutation,
            )
        )
        return EditResult(
            ok=True,
            path="append",
            badge=decision.badge,
            new_text=text,
            original_width=0.0,
            new_width=plan.new_width,
            overflow=overflow,
            scale=flow.scale,
            tracking=flow.tracking,
            lines_after=len(flow.lines),
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )

    # -- reflow internals --------------------------------------------------- #
    def _style_for_add(
        self,
        model: PageModel,
        x: float,
        y: float,
        size: float | None,
        color: ColorValue | None,
        preset: str | None,
        font_family: str | None,
        weight: int | None,
        italic: bool | None,
    ) -> tuple[float, str, int, bool, ColorValue | None]:
        hit = model.hit_test(x, y)
        if hit is not None and hit.span is not None:
            hit_size = float(hit.span.size_mupdf)
        else:
            hit_size = -1.0
        family = font_family or "Helvetica"
        if preset:
            for candidate in self.inspect.presets(model.page_index):
                if f"{candidate.name}".lower() == preset.lower():
                    size = size or float(getattr(candidate, "size", 10.5) or 10.5)
                    family = font_family or str(
                        getattr(candidate, "font_family", family) or family
                    )
                    weight = weight if weight is not None else getattr(
                        candidate, "weight", 400
                    )
                    italic = italic if italic is not None else bool(
                        getattr(candidate, "italic", False)
                    )
                    break
        else:
            if hit is not None and hit.span is not None and hit_size > 0:
                size = size or hit_size
                family = font_family or hit.span.font_mupdf.split("-")[0] or family
                weight = weight if weight is not None else (
                    700 if "Bold" in hit.span.font_mupdf else 400
                )
                italic = italic if italic is not None else bool(
                    "Italic" in hit.span.font_mupdf or "Oblique" in hit.span.font_mupdf
                )
        return (
            size or 10.5,
            family,
            weight if weight is not None else 400,
            bool(italic),
            color,
        )

    def _block_events(
        self, page_index: int, block_id: int
    ) -> tuple[PageModel, object, list[ShowEvent]]:
        if not (0 <= page_index < self.inspect.page_count):
            raise _Reject(_fail("That page no longer exists."))
        model = self.inspect.model(page_index)
        if not (0 <= block_id < len(model.blocks)):
            raise _Reject(_fail("That paragraph is no longer available."))
        block = model.blocks[block_id]
        events: list[ShowEvent] = []
        streams: set[str | None] = set()
        for line in block.lines:
            for span in line.spans:
                for char in span.chars:
                    if char.event_idx < 0:
                        raise _Reject(
                            _fail(
                                "Some text of this paragraph is not drawn by a "
                                "text-showing operation."
                            )
                        )
                    event = model.shows[char.event_idx]
                    events.append(event)
                    streams.add(event.stream_id)
        if not events:
            raise _Reject(_fail("There is no text to reflow here."))
        if len(streams) > 1:
            raise _Reject(
                _fail(
                    "This paragraph is spread over several content streams, "
                    "so it cannot be reflowed."
                )
            )
        sizes = {event.tf_size for event in events}
        if len(sizes) > 1:
            raise _Reject(
                _fail(
                    "This paragraph mixes several font sizes, "
                    "so it cannot be reflowed as one."
                )
            )
        coverage: dict[int, int] = {}
        for span in model.spans:
            for char in span.chars:
                if char.event_idx >= 0:
                    coverage[char.event_idx] = coverage.get(char.event_idx, 0) + 1
        owners: dict[int, int] = {}
        for line in block.lines:  # type: ignore[attr-defined]
            for span in line.spans:
                for char in span.chars:
                    if char.event_idx >= 0:
                        owners[char.event_idx] = owners.get(char.event_idx, 0) + 1
        shared = [
            idx for idx in owners if coverage.get(idx, 0) > owners[idx]
        ]
        if shared:
            raise _Reject(
                _fail(
                    "This paragraph shares drawing operations with other text, "
                    "so it cannot be reflowed safely."
                )
            )
        events.sort(key=lambda event: event.seq)
        return model, block, events

    def _reflow_geometry(
        self,
        model: PageModel,
        block: object,
        events: list[ShowEvent],
    ) -> tuple[BlockFrame, float, float, float]:
        """Measure the block's column in user space and classify alignment."""
        lines = block.lines  # type: ignore[attr-defined]
        page = self.session.pdf.pages[model.page_index]
        top = _page_top(page)

        def user(point: Point) -> Point:
            return (point[0], top - point[1])

        line_points: list[list[Point]] = []
        for line in lines:
            origins = [
                user(char.origin) for span in line.spans for char in span.chars
            ]
            if origins:
                line_points.append(origins)
        if not line_points:
            raise _Reject(_fail("The paragraph has no measurable baseline."))
        line_starts = [points[0] for points in line_points]

        def project(axis: Point, point: Point) -> float:
            return point[0] * axis[0] + point[1] * axis[1]

        def axis_of(points: list[Point]) -> Point:
            delta = (
                points[-1][0] - points[0][0],
                points[-1][1] - points[0][1],
            )
            length = math.hypot(*delta) or 1.0
            return (delta[0] / length, delta[1] / length)

        ax, ay = axis_of(line_points[0])
        axis = (ax, ay)
        down = self._flow_down_direction(line_starts, ax, ay)

        def mean_advance(points: list[Point]) -> float:
            if len(points) < 2:
                return 0.0
            span = max(project(axis, point) for point in points) - min(
                project(axis, point) for point in points
            )
            return span / (len(points) - 1)

        # The paragraph's own column: wrapping must stay inside the block it
        # came from (a page-wide measure would grow it into the margin or a
        # neighbouring column).
        col_left = _mode_min([project(axis, points[0]) for points in line_points])
        col_right = max(
            project(axis, points[-1]) + mean_advance(points) for points in line_points
        )
        col_width = max(1.0, col_right - col_left)

        starts = [project(axis, points[0]) for points in line_points]
        ends = [
            project(axis, points[-1]) + mean_advance(points)
            for points in line_points
        ]
        x0s = [value - col_left for value in starts]
        x1s = [value - col_left for value in ends]
        alignment = block_alignment(x0s, x1s, 0.0, col_width)

        if alignment in (Alignment.RIGHT, Alignment.CENTER):
            first_indent = 0.0
            hanging = 0.0
        else:
            first_indent = max(0.0, x0s[0])
            hanging = first_indent
            if len(x0s) > 1:
                hanging = max(0.0, _median_values(x0s[1:]))

        base = line_starts[0]
        shift = col_left - project(axis, base)
        origin = (base[0] + ax * shift, base[1] + ay * shift)
        frame = BlockFrame(
            origin=origin,
            axis=axis,
            down=down,
            alignment=alignment,
        )
        return frame, col_width, first_indent, hanging

    def _flow_down_direction(
        self, line_starts: list[Point], ax: float, ay: float
    ) -> Point:
        for start in line_starts[1:]:
            if start != line_starts[0]:
                delta = (
                    start[0] - line_starts[0][0],
                    start[1] - line_starts[0][1],
                )
                along = delta[0] * ax + delta[1] * ay
                perp = (delta[0] - along * ax, delta[1] - along * ay)
                perp_length = math.hypot(*perp)
                if perp_length > 1e-6:
                    return (perp[0] / perp_length, perp[1] / perp_length)
                length = math.hypot(*delta) or 1.0
                return (delta[0] / length, delta[1] / length)
        options = ((-ay, ax), (ay, -ax), (0.0, 0.0))
        normal = min(
            (n for n in options if n != (0.0, 0.0)),
            key=lambda n: n[1],
        )
        length = math.hypot(*normal) or 1.0
        return (normal[0] / length, normal[1] / length)

    def _block_leading(
        self,
        model: PageModel,
        block: object,
        fallback_size: float,
        axis: Point,
        down: Point,
    ) -> float:
        deltas: list[float] = []
        origins: list[Point] = []
        for line in block.lines:  # type: ignore[attr-defined]
            for span in line.spans:  # type: ignore[attr-defined]
                for char in span.chars:
                    origins.append(char.origin)
                    break
                break
        for previous, current in itertools.pairwise(origins):
            delta = (
                current[0] - previous[0],
                current[1] - previous[1],
            )
            along_down = (
                delta[0] * down[0]
                + delta[1] * down[1]
                - (delta[0] * axis[0] + delta[1] * axis[1])
                * (axis[0] * down[0] + axis[1] * down[1])
            )
            deltas.append(abs(along_down))
        if deltas:
            return sum(deltas) / len(deltas)
        return fallback_size * 1.4

    def _flow_runs_for(self, plan: _Plan, size: float) -> list[FlowRun]:
        first = plan.events[0]
        base14 = None
        if plan.runs and plan.runs[0][0].kind == "reuse" and first.font is not None:
            program = self.inspect.programs.get(first.font)
            if program is not None:
                base14 = _base14_key(program.family)
        runs: list[FlowRun] = []
        for slot, chunk, _data in plan.runs:
            runs.append(
                FlowRun(
                    style=RunStyle(
                        size=size,
                        char_width=slot.char_width,
                        base14=base14,
                        tc=first.tc,
                        tw=0.0,
                        tz=first.tz,
                        ts=first.ts,
                    ),
                    text=chunk,
                    owner=slot,
                )
            )
        return runs

    def _collisions(
        self, model: PageModel, block: object, flow_bbox: tuple[float, float, float, float]
    ) -> list[tuple[str, tuple[float, float, float, float]]]:
        page = self.session.pdf.pages[model.page_index]
        top = _page_top(page)
        obstacles: list[tuple[str, tuple[float, float, float, float]]] = []

        def flip(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
            return (rect[0], top - rect[3], rect[2], top - rect[1])

        for other in model.blocks:
            if other is block:
                continue
            obstacles.append(("text", flip(other.bbox)))
        for paint in model.paints:
            obstacles.append(
                ("paint", (paint.bbox[0], paint.bbox[1], paint.bbox[2], paint.bbox[3]))
            )
        try:
            fitz_page = self.session._mupdf[model.page_index]
        except Exception:  # pragma: no cover - no mupdf fallback
            fitz_page = None
        if fitz_page is not None:
            for image in fitz_page.get_images(full=True):
                xref = image[0]
                try:
                    rects = fitz_page.get_image_rects(xref)
                except Exception:  # pragma: no cover - malformed placement
                    continue
                for rect in rects:
                    obstacles.append(("image", flip((rect.x0, rect.y0, rect.x1, rect.y1))))
        collisions: list[tuple[str, tuple[float, float, float, float]]] = []
        for kind, rect in obstacles:
            if intersect(flow_bbox, rect, epsilon=1.0):
                collisions.append((kind, rect))
        return collisions

    def _push_down(
        self,
        plan: _Plan,
        model: PageModel,
        block: object,
        events: list[ShowEvent],
    ) -> list[_Plan]:
        """Shift sibling paragraphs down so the taller reflow cannot overlap.

        Only paragraphs living in the same content stream and overlapping the
        reflowed column horizontally are moved.  The shift is limited to the
        available space between the lowest paragraph and the bottom margin.
        """
        extras: list[_Plan] = []
        stream_ids = {event.stream_id for event in events}
        if stream_ids != {None}:
            plan.warning += "the paragraph lives in a form; no push-down; "
            return extras
        flow = plan.flow
        if flow is None:
            return extras
        old_lines = len(block.lines)  # type: ignore[attr-defined]
        new_lines = len(flow.lines)
        delta = max(0.0, new_lines - old_lines) * plan.leading
        if delta <= 0.0:
            return extras
        page = self.session.pdf.pages[model.page_index]
        top = _page_top(page)
        block_user = _flip(block.bbox, top)  # type: ignore[attr-defined]
        page_box: tuple[float, float, float, float] = (0.0, 0.0, top, top)
        for key in ("/MediaBox", "/CropBox"):
            raw = page.get(key)
            if isinstance(raw, pikepdf.Array) and len(raw) == 4:
                page_box = (
                    float(raw[0]),
                    float(raw[1]),
                    float(raw[2]),
                    float(raw[3]),
                )
                break
        bottom_margin = page_box[1] + 40.0
        candidates: list[object] = []
        lowest = bottom_margin
        for other in model.blocks:
            if other is block:
                continue
            box = _flip(other.bbox, top)
            overlap_x = not (box[2] <= block_user[0] + 1 or box[0] >= block_user[2] - 1)
            below = box[3] <= block_user[1] + 1.0
            if overlap_x and below:
                candidates.append(other)
                lowest = min(lowest, box[1])
        if not candidates:
            return extras
        if lowest - bottom_margin < delta - 0.5:
            plan.warning += (
                "push-down cannot fit inside the page margins; the paragraph "
                "may overlap; "
            )
            return extras
        for candidate in candidates:
            events_other = self._events_for_block(model, candidate)
            if not events_other:
                continue
            if any(event.stream_id is not None for event in events_other):
                plan.warning += "a following paragraph lives in a form; not pushed; "
                continue
            anchor = next(
                (
                    span
                    for line in candidate.lines  # type: ignore[attr-defined]
                    for span in line.spans  # type: ignore[attr-defined]
                    if span.chars
                ),
                None,
            )
            if anchor is None:
                continue
            try:
                sub = self._build_plan(model.page_index, anchor.id, anchor.text)
            except _Reject:
                plan.warning += "a following paragraph cannot be re-emitted; "
                continue
            old_text = "".join(
                line.text for line in candidate.lines  # type: ignore[attr-defined]
            )
            geo = self._reflow_geometry(model, candidate, events_other)
            sub_frame, sub_available, sub_indent, sub_hanging = geo
            sub.events = events_other
            sub.stream_id = None
            flow_runs = self._flow_runs_for(sub, sub.events[0].tf_size)
            sub_lines, _too_wide = wrap_paragraph(
                flow_runs,
                sub_available,
                first_indent=sub_indent,
                hanging=sub_hanging,
            )
            sub_flow = FlowBlock(lines=sub_lines)
            align_lines(sub_lines, sub_available, sub_frame.alignment)
            sub.frame = sub_frame
            sub.flow = sub_flow
            sub.leading = plan.leading
            sub.pushed = delta
            sub.noop = False
            sub.reflow = True
            sub.old_text = old_text
            extras.append(sub)
        if extras:
            plan.pushed = delta
        return extras

    def _events_for_block(
        self, model: PageModel, block: object
    ) -> list[ShowEvent]:
        events: list[ShowEvent] = []
        for line in block.lines:  # type: ignore[attr-defined]
            for span in line.spans:  # type: ignore[attr-defined]
                for char in span.chars:
                    if char.event_idx < 0:
                        return []
                    events.append(model.shows[char.event_idx])
        if not events:
            return []
        return sorted(set(events), key=lambda event: event.seq)

    def _commit_reflow(self, plans: list[_Plan]) -> None:
        pdf = self.session.pdf
        first_plan = plans[0]
        targets = self._targets(first_plan)
        embed_slots: list[_Slot] = []
        seen: set[str] = set()
        for plan in plans:
            for slot in plan.embed_slots():
                if slot.key not in seen:
                    seen.add(slot.key)
                    embed_slots.append(slot)
        if embed_slots:
            self._assign_names(first_plan, embed_slots)
        prepared = self._prepare_reflow(plans, targets)
        if embed_slots:
            fonts = self._ensure_fonts(first_plan)
            for slot in embed_slots:
                if slot.embed is None or slot.tounicode is None:
                    continue  # pragma: no cover - slots always carry both
                fonts[pikepdf.Name("/" + slot.effective_name)] = _font_object(
                    pdf, slot.embed, slot.tounicode
                )
        for plan in plans:
            for slot, _chunk, _data in plan.runs:
                if slot.kind == "reuse":
                    self._apply_reuse_tounicode(slot)
        for target, data in prepared:
            if target is first_plan.page:
                first_plan.page.Contents = pdf.make_stream(data)
            else:
                target.write(data)

    def _prepare_reflow(
        self,
        plans: list[_Plan],
        targets: dict[str | None, Any],
    ) -> list[tuple[Any, bytes]]:
        stream_id = plans[0].stream_id
        target = targets[stream_id]
        instructions = list(pikepdf.parse_content_stream(target))
        wanted: set[int] = set()
        for plan in plans:
            for event in plan.events:
                if not (0 <= event.instr < len(instructions)):
                    raise _PlanError("the content stream changed")
                if _op(instructions[event.instr]) not in _SHOW_OPS:
                    raise _PlanError("the content stream changed")
                wanted.add(event.instr)
        out: list[Any] = []
        for index, instruction in enumerate(instructions):
            if index in wanted:
                out.append(
                    (
                        _blank_operands(_operands(instruction), _op(instruction)),
                        _op(instruction),
                    )
                )
            else:
                out.append(instruction)
        final_ctm = _final_ctm(out)
        appended: list[Any] = []
        for plan in plans:
            appended.extend(self._flow_block_ops(plan, final_ctm))
        return [(target, pikepdf.unparse_content_stream(out + appended))]

    def _flow_block_ops(
        self, plan: _Plan, final_ctm: Matrix
    ) -> list[tuple[list[Any], str]]:
        first = plan.events[0] if plan.events else None
        flow = plan.flow
        frame = plan.frame
        if flow is None or frame is None:
            raise _PlanError("missing reflow geometry")
        ax, ay = frame.axis
        dx, dy = frame.down
        ox, oy = frame.origin
        size = first.tf_size if first is not None else flow.lines[0].runs[0].style.size
        fill = first.fill if first is not None else getattr(plan, "add_color", None)
        local_to_user = (ax, ay, dx, dy, ox, oy)
        ops: list[tuple[list[Any], str]] = [([], "q")]
        ops.append((list(mat_mul(local_to_user, _mat_inv(final_ctm))), "cm"))
        if fill is not None:
            color_ops, _warning = _color_ops(fill)
            ops.extend(color_ops)
        ops.append(([], "BT"))
        last_tw = 0.0
        current_fill = fill
        for line in flow.lines:
            depth = line.y * plan.leading + plan.pushed
            # The frame's second axis points *down* the page (successive
            # baselines); glyph space is y-up, so flip it or text renders
            # upside down.
            ops.append(([1.0, 0.0, 0.0, -1.0, line.x, depth], "Tm"))
            justify_tw = line.space_extra if line.justify else 0.0
            if justify_tw != last_tw:
                ops.append(([justify_tw], "Tw"))
                last_tw = justify_tw
            for run in line.runs:
                slot = run.owner
                name = slot.effective_name if slot is not None else ""
                run_fill = run.style.fill if run.style.fill is not None else fill
                if run_fill is not None and run_fill != current_fill:
                    color_ops, _warning = _color_ops(run_fill)
                    ops.extend(color_ops)
                    current_fill = run_fill
                run_size = run.style.size or size
                ops.append(([pikepdf.Name("/" + name), run_size], "Tf"))
                tz = run.style.tz * flow.scale
                if tz != 100.0:
                    ops.append(([tz], "Tz"))
                tc = flow.tracking if flow.tracking else run.style.tc
                if tc:
                    ops.append(([tc], "Tc"))
                if run.style.ts:
                    ops.append(([run.style.ts], "Ts"))
                if slot is None:
                    raise _PlanError("a flow run lost its slot")
                if line.justify and run.text:
                    self._justified_ops(ops, slot, run.text, line.space_extra)
                elif run.text:
                    ops.append(([pikepdf.String(_encode_run(slot, run.text))], "Tj"))
        ops.append(([], "ET"))
        ops.append(([], "Q"))
        return ops

    def _justified_ops(
        self,
        ops: list[tuple[list[Any], str]],
        slot: _Slot,
        text: str,
        space_extra: float,
    ) -> None:
        clean = text.rstrip(" ")
        encoded = _encode_run(slot, clean) if clean else b""
        if clean:
            ops.append(([pikepdf.String(encoded)], "Tj"))

    # -- helpers ------------------------------------------------------------ #
    def _post_commit(self, page_index: int) -> None:
        """Let OCR text-layer lines that were just rewritten become visible.

        Runs inside the same undo step, before the view is refreshed, so the
        original scan pixels are still available for covering/inpainting.
        """
        from offlinepdf.core.ocr.textlayer import reveal_changed

        reveal_changed(self.session, page_index)

    def _after_mutation(self) -> None:
        self.inspect.invalidate()
        if self._on_changed is not None:
            self._on_changed()


# --------------------------------------------------------------------------- #
# instruction helpers
# --------------------------------------------------------------------------- #
def _show_operands(operands: list[Any], operator: str, data: bytes) -> list[Any]:
    text = pikepdf.String(data)
    if operator == "TJ":
        return [pikepdf.Array([text])]
    if operator == '"':
        return [*operands[:2], text]
    return [text]


def _flip(
    rect: tuple[float, float, float, float], top: float
) -> tuple[float, float, float, float]:
    return (rect[0], top - rect[3], rect[2], top - rect[1])


def _median_values(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _mode_min(values: list[float]) -> float:
    if not values:
        return 0.0
    counts: dict[float, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    best = max(counts.values())
    return min(value for value, count in counts.items() if count == best)


def _blank_operands(operands: list[Any], operator: str) -> list[Any]:
    if operator == "TJ":
        return [pikepdf.Array()]
    if operator == '"':
        return [*operands[:2], pikepdf.String(b"")]
    return [pikepdf.String(b"")]


def _encode_run(slot: _Slot, text: str) -> bytes:
    return b"".join(slot.char_bytes[char] for char in text)


def _font_object(
    pdf: pikepdf.Pdf, embedded: _Embedded, tounicode: _ToUnicodePlan
) -> pikepdf.Dictionary:
    font_file = pdf.make_stream(embedded.data)
    descriptor = pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"),
        FontName=pikepdf.Name("/" + embedded.ps_name),
        Flags=embedded.flags,
        FontBBox=pikepdf.Array([float(value) for value in embedded.bbox]),
        ItalicAngle=embedded.italic_angle,
        Ascent=embedded.ascent,
        Descent=embedded.descent,
        CapHeight=embedded.cap_height,
        StemV=embedded.stem_v,
        MissingWidth=0,
        FontFile2=font_file,
    )
    cid_map = struct.pack(f">{len(embedded.cid_to_gid)}H", *embedded.cid_to_gid)
    cid_font = pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"),
        Subtype=pikepdf.Name("/CIDFontType2"),
        BaseFont=pikepdf.Name("/" + embedded.ps_name),
        CIDSystemInfo=pikepdf.Dictionary(
            Registry=pikepdf.String(b"Adobe"),
            Ordering=pikepdf.String(b"Identity"),
            Supplement=0,
        ),
        FontDescriptor=descriptor,
        DW=1000,
        W=pikepdf.Array(
            [1, pikepdf.Array([float(value) for value in embedded.widths[1:]])]
        ),
        CIDToGIDMap=pdf.make_stream(cid_map),
    )
    return pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"),
        Subtype=pikepdf.Name("/Type0"),
        BaseFont=pikepdf.Name("/" + embedded.ps_name),
        Encoding=pikepdf.Name("/Identity-H"),
        DescendantFonts=pikepdf.Array([cid_font]),
        ToUnicode=pdf.make_stream(_tounicode_cmap(tounicode.pairs)),
    )


def _sanitize(text: str) -> str:
    return (
        text.replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\t", " ")
    )


_RTL_RANGES = ((0x0590, 0x08FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))


def _is_rtl(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _RTL_RANGES)


def _is_neutral(char: str) -> bool:
    return char.isspace() or char in ".,;:!?'\"()[]-–—"


def _visual_rtl(text: str) -> str:
    """Reorder logical text into the visual order PDF streams expect.

    MuPDF extraction reorders right-to-left runs back to logical order, so
    every RTL run must be written to the content stream in visual order
    (reversed).  Neutrals between two RTL characters join the run, so words
    reverse together; LTR neighbours keep writing left-to-right.
    """
    chars = list(text)
    length = len(chars)
    index = 0
    runs: list[tuple[int, int]] = []
    while index < length:
        if not _is_rtl(chars[index]):
            index += 1
            continue
        end = index + 1
        while end < length:
            if _is_rtl(chars[end]):
                end += 1
                continue
            if _is_neutral(chars[end]):
                probe = end
                while probe < length and _is_neutral(chars[probe]):
                    probe += 1
                if probe < length and _is_rtl(chars[probe]):
                    end = probe
                    continue
                break
            break
        runs.append((index, end))
        index = end
    for start, end in reversed(runs):
        chars[start:end] = chars[start:end][::-1]
    return "".join(chars)


def _badge(plan: _Plan) -> str:
    if plan.reuse_pct >= 100:
        return "original font reused (100%)"
    return f"{plan.decision.badge} ({plan.reuse_pct}% original)"


def _fail(message: str, decision: FontDecision | None = None) -> EditResult:
    return EditResult(
        ok=False,
        warning=message,
        decision=decision,
        badge=decision.badge if decision is not None else "",
    )

