"""Font program inspection: real weight/italic/family/coverage.

The PDF FontDescriptor flags are a known liar (subset fonts often inherit
wrong flags), so whenever a font program is embedded we read the truth out of
it with fontTools:

* OS/2 usWeightClass       -> weight (100..900)
* post italicAngle / fsSelection bits 0/9 -> italic
* name table ids 1/2       -> family + style
* post isFixedPitch        -> monospaced
* cmap                     -> unicode coverage (for glyph-missing detection)

Non-embedded fonts (base 14 and friends) fall back to BaseFont name
heuristics; their coverage is a conservative WinAnsi estimate for the
Helvetica/Times/Courier families and unknown for everything else.

Results are cached per font object (objgen) inside one document session.
"""

from __future__ import annotations

import dataclasses
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass

import pikepdf
from fontTools.cffLib import CFFFontSet
from fontTools.ttLib import TTFont, TTLibError

from offlinepdf.core.content_stream import FontRef

try:
    from fontTools.agl import toUnicode as _agl_to_unicode
except ImportError:  # pragma: no cover - agl ships with fontTools
    _agl_to_unicode = None

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_TRAILING_PS = re.compile(r"(?:PSMT|MT|PS)$")

# style words that only describe the style, never the family
_STYLE_WORDS = frozenset(
    {
        "bold",
        "italic",
        "oblique",
        "regular",
        "roman",
        "book",
        "medium",
        "light",
        "thin",
        "black",
        "heavy",
        "semibold",
        "demibold",
        "extrabold",
        "ultrabold",
        "extralight",
        "ultralight",
    }
)

# longest first so "extrabold" wins over "bold"
_WEIGHT_WORDS: tuple[tuple[str, int], ...] = (
    ("extrablack", 950),
    ("ultrablack", 950),
    ("extrabold", 800),
    ("ultrabold", 800),
    ("semibold", 600),
    ("demibold", 600),
    ("black", 900),
    ("heavy", 900),
    ("bold", 700),
    ("medium", 500),
    ("light", 300),
    ("extralight", 200),
    ("ultralight", 200),
    ("thin", 100),
    ("regular", 400),
    ("roman", 400),
    ("book", 400),
)

_STANDARD_14 = (
    "helvetica",
    "times",
    "courier",
    "symbol",
    "zapfdingbats",
)

_TYPE1_WEIGHT = re.compile(rb"/Weight\s*\(([^)]*)\)")
_TYPE1_ITALIC_ANGLE = re.compile(rb"/ItalicAngle\s*(-?[\d.]+)")


def _weight_from_text(text: str) -> int | None:
    lowered = text.lower().replace(" ", "").replace("-", "").replace("_", "")
    for word, weight in _WEIGHT_WORDS:
        if word in lowered:
            return weight
    return None


def family_from_base(base_font: str) -> str:
    """Public helper: family guess from a BaseFont-style name."""
    return _family_from_base(base_font)


def strip_subset_prefix(base_font: str) -> str:
    """Public helper: remove the ``ABCDEF+`` subset tag from a font name."""
    return _SUBSET_PREFIX.sub("", base_font)


def weight_from_base(base_font: str) -> int | None:
    """Public helper: weight guess from a BaseFont-style name (None if bland)."""
    return _weight_from_text(base_font)


def _family_from_base(base_font: str) -> str:
    name = _SUBSET_PREFIX.sub("", base_font)
    name = _TRAILING_PS.sub("", name)
    if "-" in name:
        parts = [p for p in name.split("-") if p]
        while len(parts) > 1 and parts[-1].lower() in _STYLE_WORDS:
            parts.pop()
        name = "-".join(parts)
    return name or base_font or "Unknown"


def _style_from_base(base_font: str) -> str:
    name = _SUBSET_PREFIX.sub("", base_font)
    lowered = name.lower()
    italic = "italic" in lowered or "oblique" in lowered
    weight = _weight_from_text(name) or 400
    parts: list[str] = []
    if weight >= 600:
        parts.append("Bold" if weight == 700 else f"W{weight}")
    if italic:
        parts.append("Italic")
    return " ".join(parts) or "Regular"


def _base14_coverage(base_font: str) -> frozenset[int] | None:
    """Conservative WinAnsi-ish coverage estimate for base-14 families."""
    lowered = _SUBSET_PREFIX.sub("", base_font).lower()
    if any(lowered.startswith(family) for family in ("symbol", "zapfdingbats")):
        return None
    if not any(lowered.startswith(family) for family in _STANDARD_14):
        return None
    points = set(range(0x20, 0x7F))  # ASCII
    points.update(range(0xA0, 0x100))  # Latin-1 supplement
    points.update(
        {
            0x2018,
            0x2019,
            0x201A,
            0x201C,
            0x201D,
            0x201E,
            0x2013,
            0x2014,
            0x2022,
            0x2026,
            0x2039,
            0x203A,
            0x20AC,
            0x2212,
        }
    )
    return frozenset(points)


@dataclass(frozen=True)
class FontProgram:
    """What we learned about one font object."""

    objgen: str
    base_font: str  # subset prefix stripped
    family: str
    style: str
    weight: int  # 100..900
    italic: bool
    monospace: bool
    embedded: bool
    source: str  # "os2" | "cff" | "type1" | "name" | "base14" | "default"
    coverage: frozenset[int] | None = None

    @property
    def coverage_known(self) -> bool:
        return self.coverage is not None

    def covers(self, text: str) -> bool:
        if self.coverage is None:
            return True
        return all(ord(char) in self.coverage for char in text)


def _from_name(ref: FontRef, source: str) -> FontProgram:
    base = _SUBSET_PREFIX.sub("", ref.base_font)
    weight = _weight_from_text(base)
    if weight is None:
        # descriptor can still tell the truth about weight-ish flags
        weight = 700 if (ref.descriptor_flags or 0) & (1 << 18) else 400
    italic = bool(ref.italic_angle) or bool("italic" in base.lower() or "oblique" in base.lower())
    monospace = "courier" in base.lower() or "mono" in base.lower()
    coverage: frozenset[int] | None
    if not ref.embedded:
        coverage = _base14_coverage(base)
        if coverage is not None and source == "name":
            source = "base14"
    else:
        coverage = None
    return FontProgram(
        objgen=ref.objgen,
        base_font=base,
        family=_family_from_base(ref.base_font),
        style=_style_from_base(ref.base_font),
        weight=weight,
        italic=italic,
        monospace=monospace,
        embedded=ref.embedded,
        source=source,
        coverage=coverage,
    )


def _descriptor_of(font_dict: pikepdf.Dictionary) -> pikepdf.Dictionary | None:
    subtype = str(font_dict.get("/Subtype") or "")
    if subtype == "/Type0":
        descendants = font_dict.get("/DescendantFonts")
        if isinstance(descendants, pikepdf.Array) and len(descendants) > 0:
            first = descendants[0]
            if isinstance(first, pikepdf.Dictionary):
                descriptor = first.get("/FontDescriptor")
                if isinstance(descriptor, pikepdf.Dictionary):
                    return descriptor
        return None
    descriptor = font_dict.get("/FontDescriptor")
    if isinstance(descriptor, pikepdf.Dictionary):
        return descriptor
    # CIDFontType2 sometimes nests the descriptor on the descendant directly
    return None


def _font_program_bytes(descriptor: pikepdf.Dictionary) -> tuple[bytes, str] | None:
    for key, kind in (
        ("/FontFile3", "cff"),
        ("/FontFile2", "ttf"),
        ("/FontFile", "type1"),
    ):
        stream = descriptor.get(key)
        if isinstance(stream, pikepdf.Stream):
            sub = stream.get("/Subtype")
            sub_name = str(sub).lstrip("/") if isinstance(sub, pikepdf.Name) else ""
            if key == "/FontFile3":
                if sub_name == "OpenType":
                    kind = "ttf"
                elif sub_name in ("Type1C", "CIDFontType0C"):
                    kind = "cff"
            try:
                data = stream.read_bytes()
            except Exception:
                continue
            if data:
                return data, kind
    return None


def _clamp_weight(value: object) -> int | None:
    try:
        weight = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    if weight <= 0:
        return None
    if weight < 100:
        weight *= 10  # some old fonts report 4 / 7 instead of 400 / 700
    return max(100, min(900, weight))


def _parse_ttf(
    data: bytes, ref: FontRef, base: str
) -> tuple[FontProgram, str] | None:
    try:
        font = TTFont(io.BytesIO(data), lazy=True)
    except (TTLibError, OSError, ValueError):
        return None
    try:
        weight: int | None = None
        source = "name"
        if "OS/2" in font:
            weight = _clamp_weight(font["OS/2"].usWeightClass)
            source = "os2"
        italic = False
        if "post" in font and getattr(font["post"], "italicAngle", False):
            italic = True
        if "OS/2" in font:
            fs_selection = int(getattr(font["OS/2"], "fsSelection", 0))
            if fs_selection & 0x1 or fs_selection & 0x200:
                italic = True
        if not italic:
            italic = "italic" in base.lower() or "oblique" in base.lower()
        if weight is None:
            weight = _weight_from_text(base) or 400
            source = "name"

        family = ""
        style = ""
        if "name" in font:
            name_table = font["name"]
            family = name_table.getDebugName(1) or ""
            style = name_table.getDebugName(2) or ""
            if not family:
                family = name_table.getDebugName(4) or ""
        family = _family_from_base(family) if family else _family_from_base(base)
        if not style:
            style = _style_from_base(base)

        monospace = False
        if "post" in font and getattr(font["post"], "isFixedPitch", False):
            monospace = True
        elif "OS/2" in font:
            panose = getattr(font["OS/2"], "panose", None)
            if panose is not None and getattr(panose, "proportion", 0) == 9:
                monospace = True
        if not monospace:
            monospace = "courier" in base.lower() or "mono" in base.lower()

        coverage: frozenset[int] | None = None
        try:
            cmap = font.getBestCmap()
        except (TTLibError, KeyError, ValueError):
            cmap = None
        if cmap:
            coverage = frozenset(cmap)

        program = FontProgram(
            objgen=ref.objgen,
            base_font=base,
            family=family,
            style=style,
            weight=weight,
            italic=italic,
            monospace=monospace,
            embedded=True,
            source=source,
            coverage=coverage,
        )
        return program, source
    finally:
        font.close()


def _parse_cff(data: bytes, ref: FontRef, base: str) -> tuple[FontProgram, str] | None:
    try:
        cff = CFFFontSet()
        cff.decompile(io.BytesIO(data), None)
        font_name = cff.fontNames[0]
        top = cff[font_name]
    except Exception:
        return None
    raw = getattr(top, "rawDict", {}) or {}
    weight = _clamp_weight(raw.get("Weight")) or _weight_from_text(str(raw.get("FullName", "")))
    source = "cff" if weight else "name"
    weight = weight or _weight_from_text(base) or 400
    italic = False
    try:
        italic = float(raw.get("ItalicAngle") or 0) != 0
    except (TypeError, ValueError):
        italic = False
    if not italic:
        italic = "italic" in base.lower() or "oblique" in base.lower()

    coverage: frozenset[int] | None = None
    charset = getattr(top, "charset", None)
    if charset and _agl_to_unicode is not None:
        points = set()
        for glyph_name in charset:
            if glyph_name == ".notdef":
                continue
            text = _agl_to_unicode(glyph_name)
            if text and len(text) == 1:
                points.add(ord(text))
        if points:
            coverage = frozenset(points)

    return (
        FontProgram(
            objgen=ref.objgen,
            base_font=base,
            family=_family_from_base(str(raw.get("FamilyName") or base)),
            style=_style_from_base(base),
            weight=weight,
            italic=italic,
            monospace=False,
            embedded=True,
            source=source,
            coverage=coverage,
        ),
        source,
    )


def _parse_type1(data: bytes, ref: FontRef, base: str) -> tuple[FontProgram, str] | None:
    weight: int | None = None
    italic = False
    match = _TYPE1_WEIGHT.search(data)
    if match:
        weight = _weight_from_text(match.group(1).decode("latin-1", "replace"))
    match = _TYPE1_ITALIC_ANGLE.search(data)
    if match:
        try:
            italic = float(match.group(1)) != 0
        except ValueError:
            italic = False
    if weight is None and not italic:
        return None
    name_program = _from_name(ref, "type1")
    return (
        FontProgram(
            objgen=ref.objgen,
            base_font=name_program.base_font,
            family=name_program.family,
            style=name_program.style,
            weight=weight if weight is not None else name_program.weight,
            italic=italic or name_program.italic,
            monospace=name_program.monospace,
            embedded=name_program.embedded,
            source="type1",
            coverage=name_program.coverage,
        ),
        "type1",
    )


def _from_program(
    ref: FontRef, font_dict: pikepdf.Dictionary | None
) -> FontProgram:
    base = _SUBSET_PREFIX.sub("", ref.base_font)
    if font_dict is not None:
        descriptor = _descriptor_of(font_dict)
        if descriptor is not None:
            program_bytes = _font_program_bytes(descriptor)
            if program_bytes is not None:
                data, kind = program_bytes
                parsed: tuple[FontProgram, str] | None = None
                if kind == "ttf":
                    parsed = _parse_ttf(data, ref, base)
                elif kind == "cff":
                    parsed = _parse_cff(data, ref, base)
                elif kind == "type1":
                    parsed = _parse_type1(data, ref, base)
                if parsed is not None:
                    return parsed[0]
    if ref.embedded:
        return _from_name(ref, "name")
    return _from_name(ref, "name")


class FontPrograms:
    """Per-document cache of FontProgram, keyed by font object generation."""

    def __init__(
        self,
        fonts: dict[str, FontRef] | None = None,
        font_dicts: dict[str, pikepdf.Dictionary] | None = None,
    ) -> None:
        self._fonts: dict[str, FontRef] = dict(fonts or {})
        self._font_dicts: dict[str, pikepdf.Dictionary] = dict(font_dicts or {})
        self._cache: dict[str, FontProgram] = {}

    def add_fonts(
        self,
        fonts: dict[str, FontRef],
        font_dicts: dict[str, pikepdf.Dictionary],
    ) -> None:
        """Merge another page's font resources into the shared cache."""
        for objgen, ref in fonts.items():
            self._fonts.setdefault(objgen, ref)
        self._font_dicts.update(font_dicts)

    def get(self, ref: FontRef) -> FontProgram:
        cached = self._cache.get(ref.objgen)
        if cached is not None:
            return cached
        font_dict = self._font_dicts.get(ref.objgen)
        program = _from_program(ref, font_dict)
        if program.coverage is None and font_dict is not None and program.embedded:
            # Subset fonts usually carry no Unicode cmap (their cmap is keyed by
            # the PDF's own codes), but the PDF's ToUnicode map says exactly
            # which characters the embedded glyphs show.
            codes = present_codes(font_dict)
            if codes:
                program = dataclasses.replace(program, coverage=frozenset(codes))
        self._cache[ref.objgen] = program
        return program

    def by_objgen(self, objgen: str) -> FontProgram | None:
        ref = self._fonts.get(objgen)
        if ref is None:
            return None
        return self.get(ref)

    def all(self) -> list[FontProgram]:
        return [self.get(ref) for ref in self._fonts.values()]

    def __len__(self) -> int:
        return len(self._fonts)

    def __contains__(self, objgen: object) -> bool:
        return objgen in self._fonts

_CMAP_HEX = re.compile(rb"<([0-9A-Fa-f]*)>")
_CMAP_RANGE = re.compile(
    rb"<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*(?:<[0-9A-Fa-f]*>|\[[^\]]*\])"
)


def _cmap_unicode(hex_digits: bytes) -> str:
    raw = bytes.fromhex(hex_digits.decode("ascii"))
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return ""


def tounicode_pairs(font_dict: pikepdf.Dictionary) -> list[tuple[int, bytes]]:
    """Every one-character ``(code point, code)`` mapping of /ToUnicode."""
    return [(cp, code) for code, cp in _tounicode_raw(font_dict)]


def tounicode_codes(font_dict: pikepdf.Dictionary) -> dict[int, bytes]:
    """Code point -> character code(s), read from the font's /ToUnicode CMap.

    This is the PDF's own record of what each code shows, so it is the
    reliable way to re-encode new text with an embedded *subset* font (whose
    internal cmap is keyed by codes, not Unicode).  Only one-character
    mappings are kept; when several codes show the same character the
    lowest code wins.  Empty when there is no (readable) ToUnicode.
    """
    out: dict[int, bytes] = {}
    for cp, code in tounicode_pairs(font_dict):
        if cp not in out or code < out[cp]:
            out[cp] = code
    return out


def _tounicode_raw(font_dict: pikepdf.Dictionary) -> list[tuple[bytes, int]]:
    stream = font_dict.get("/ToUnicode")
    if not isinstance(stream, pikepdf.Stream):
        return []
    try:
        data = stream.read_bytes()
    except Exception:
        return []
    pairs: list[tuple[bytes, str]] = []
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        hexes = _CMAP_HEX.findall(block)
        for src, dst in zip(hexes[0::2], hexes[1::2], strict=False):
            pairs.append((bytes.fromhex(src.decode("ascii")), _cmap_unicode(dst)))
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for line in _CMAP_RANGE.findall(block):
            hexes = _CMAP_HEX.findall(line)
            if len(hexes) < 3:
                continue
            low, high = hexes[0], hexes[1]
            width = len(low) // 2
            start, end = int(low or b"0", 16), int(high or b"0", 16)
            if end < start or end - start > 0xFFFF:
                continue
            if b"[" in line:
                targets = [_cmap_unicode(h) for h in hexes[2:]]
                for offset, text in enumerate(targets[: end - start + 1]):
                    pairs.append(((start + offset).to_bytes(width, "big"), text))
            else:
                base = _cmap_unicode(hexes[2])
                if len(base) != 1:
                    continue
                for offset in range(end - start + 1):
                    code = (start + offset).to_bytes(width, "big")
                    pairs.append((code, chr(ord(base) + offset)))
    return [
        (code, ord(text)) for code, text in pairs if len(text) == 1 and text != "\ufffd"
    ]


def present_codes(font_dict: pikepdf.Dictionary) -> dict[int, bytes]:
    """Code point -> code for characters whose glyph really is in the program.

    ToUnicode may list codes a subset dropped, and several codes may show the
    same character (alternates), so every candidate is checked and the lowest
    *present* code wins; reusing a dropped glyph would draw .notdef boxes.
    TrueType programs are checked glyph by glyph (an empty outline counts as
    missing except for whitespace); other formats are trusted as declared.
    """
    candidates = tounicode_pairs(font_dict)
    codes = tounicode_codes(font_dict)
    if not candidates:
        return codes
    packed = program_bytes(font_dict)
    if packed is None or packed[1] != "ttf":
        return codes
    try:
        font = TTFont(io.BytesIO(packed[0]), lazy=True)
    except (TTLibError, OSError, ValueError):
        return codes
    try:
        order = font.getGlyphOrder()
        glyf = font.get("glyf", None)

        def has_outline(name: str | None, cp: int) -> bool:
            if name is None:
                return False
            if chr(cp).isspace():
                return True
            if glyf is None:
                return True
            try:
                return glyf[name].numberOfContours != 0
            except (KeyError, AttributeError):
                return False

        kept: dict[int, bytes] = {}

        def keep(cp: int, code: bytes) -> None:
            if cp not in kept or code < kept[cp]:
                kept[cp] = code

        if str(font_dict.get("/Subtype") or "") == "/Type0":
            descendant = font_dict.get("/DescendantFonts")
            cid_font = descendant[0] if isinstance(descendant, pikepdf.Array) else None
            mapping = cid_font.get("/CIDToGIDMap") if cid_font is not None else None
            table: bytes | None = (
                mapping.read_bytes() if isinstance(mapping, pikepdf.Stream) else None
            )
            for cp, code in candidates:
                cid = int.from_bytes(code, "big")
                if table is not None:
                    if 2 * cid + 2 > len(table):
                        continue
                    gid = int.from_bytes(table[2 * cid : 2 * cid + 2], "big")
                else:
                    gid = cid
                if 0 < gid < len(order) and has_outline(order[gid], cp):
                    keep(cp, code)
            return kept
        subtables = list(font["cmap"].tables) if "cmap" in font else []
        for cp, code in candidates:
            value = int.from_bytes(code, "big")
            name = None
            for sub in subtables:
                name = sub.cmap.get(value) or sub.cmap.get(0xF000 + value)
                if sub.platformID == 3 and sub.platEncID in (1, 10):
                    name = sub.cmap.get(cp) or name
                if name is not None:
                    break
            if has_outline(name, cp):
                keep(cp, code)
        return kept if subtables else codes
    except Exception:
        return codes
    finally:
        font.close()


def program_bytes(font_dict: pikepdf.Dictionary) -> tuple[bytes, str] | None:
    """Public helper: the embedded font program (bytes, kind) of a font dict."""
    descriptor = _descriptor_of(font_dict)
    if descriptor is None:
        return None
    return _font_program_bytes(descriptor)


def codepoint_coverage(data: bytes, kind: str) -> frozenset[int] | None:
    """Unicode code points actually present in a font program."""
    if kind == "type1":
        try:
            from fontTools.t1Lib import T1Font

            font = T1Font(io.BytesIO(data))
            char_strings = getattr(font, "charStrings", None)
            if char_strings is None:
                return None
            names = set(char_strings.keys())
            from fontTools.agl import toUnicode as agl

            points: set[int] = set()
            for name in names:
                try:
                    char = agl[name]
                except Exception:
                    continue
                if len(char) == 1:
                    points.add(ord(char))
            return frozenset(points) if points else None
        except Exception:
            return None
    try:
        font = TTFont(io.BytesIO(data), lazy=True)
        cmap = font.getBestCmap()
        font.close()
    except (TTLibError, OSError, ValueError, KeyError):
        return None
    if not cmap:
        return None
    return frozenset(cmap)


def advance_widths(
    data: bytes, kind: str, codepoints: Iterable[int]
) -> dict[int, int]:
    """Advance widths in 1/1000 em for each code point ({} when unknown)."""
    if kind == "type1":
        return {}
    try:
        font = TTFont(io.BytesIO(data), lazy=True)
        cmap = font.getBestCmap() or {}
        hmtx = font["hmtx"]
        upm = int(font["head"].unitsPerEm or 1000)
        metrics = hmtx.metrics
        out: dict[int, int] = {}
        for codepoint in set(codepoints):
            glyph = cmap.get(codepoint)
            if glyph is None or glyph not in metrics:
                continue
            out[codepoint] = round(metrics[glyph][0] * 1000.0 / upm, 2)
        font.close()
    except (TTLibError, OSError, ValueError, KeyError):
        return {}
    return out
