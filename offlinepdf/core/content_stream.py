"""Content stream inspection for style detection (Milestone M2).

Walks a page's content stream(s) plus nested form XObjects with a small
graphics/text state machine and records:

* every show-text operation (:class:`ShowEvent`) with its exact pen origin,
  glyph count and a snapshot of the text + graphics state at that moment;
* every painted path (:class:`PaintRecord`) so decorations (underline,
  strike-through, highlight) and backgrounds can be explained later.

The parser works in **PDF user space** (origin bottom-left, y up) exactly as
the file describes it; conversion to the viewer's y-down space is done by
:mod:`offlinepdf.core.text_model`.

State semantics implemented here (PDF 32000-1 8.3/9.4):

* ``q``/``Q`` save the graphics state *including* the text state parameters
  (``Tc``, ``Tw``, ``Tz``, ``TL``, ``Tr``, ``Ts``, ``Tf``) but **not** the
  text matrices ``Tm``/``Tlm`` and not the path under construction;
* ``cm``/``Td``/``Tm`` follow the row-vector convention: the new matrix is
  applied to points *before* the one it concatenates with;
* the pen origin of a show is ``CTM x Tm x (0, Ts)`` - the rise is applied
  inside the text matrix and is not scaled by the font size;
* show-text operations never advance ``Tlm``, only ``Tm``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import pikepdf

Matrix = tuple[float, float, float, float, float, float]
Rect = tuple[float, float, float, float]
Point = tuple[float, float]

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_MAX_GSTATE_DEPTH = 128
_MAX_FORM_DEPTH = 16

_CODESPACE_RE = re.compile(r"begincodespacerange(.*?)endcodespacerange", re.S)
_HEX_RANGE_RE = re.compile(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")


# --------------------------------------------------------------------------- #
# matrix helpers (row-vector convention: p' = p x M, "a applied first")
# --------------------------------------------------------------------------- #
def mat_mul(a: Matrix, b: Matrix) -> Matrix:
    """Product ``a . b`` where ``a`` transforms points before ``b``."""
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (
        a0 * b0 + a1 * b2,
        a0 * b1 + a1 * b3,
        a2 * b0 + a3 * b2,
        a2 * b1 + a3 * b3,
        a4 * b0 + a5 * b2 + b4,
        a4 * b1 + a5 * b3 + b5,
    )


def mat_apply(m: Matrix, x: float, y: float) -> Point:
    """Transform ``(x, y)`` by ``m``."""
    a, b, c, d, e, f = m
    return (x * a + y * c + e, x * b + y * d + f)


def mat_scale(m: Matrix) -> float:
    """Length of the transformed x-axis (approximate uniform scale)."""
    a, b, _, _, _, _ = m
    return (a * a + b * b) ** 0.5


def mat_is_identity(m: Matrix, tol: float = 1e-9) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(m, IDENTITY, strict=True))


# --------------------------------------------------------------------------- #
# data types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ColorValue:
    """A colour exactly as authored in the content stream.

    ``space`` is the PDF colour space (``DeviceRGB``, ``DeviceCMYK``,
    ``Separation``, ``ICCBased``, ``Pattern``, ...); ``components`` are the
    raw operands (0..1); ``n`` is the component count hint for ``ICCBased``
    and ``DeviceN``; ``base_space`` is the base space of separation/indexed/
    uncoloured-pattern spaces.
    """

    space: str
    components: tuple[float, ...] = ()
    pattern: str | None = None
    n: int | None = None
    base_space: str | None = None

    @property
    def key(self) -> tuple[object, ...]:
        return (
            self.space,
            tuple(round(c, 6) for c in self.components),
            self.pattern,
            self.n,
            self.base_space,
        )


BLACK: ColorValue = ColorValue("DeviceGray", (0.0,))


@dataclass(frozen=True)
class FontRef:
    """Resolved identity of a font used by a show operation."""

    res_name: str
    objgen: str
    subtype: str
    base_font: str
    embedded: bool
    descriptor_flags: int | None
    italic_angle: float | None
    code_lengths: tuple[int, ...]
    differences: tuple[tuple[int, str], ...] = ()
    encoding: str = ""


@dataclass(frozen=True)
class ShowEvent:
    """One show-text operation with the state at the moment it ran."""

    seq: int
    origin: Point
    origin_known: bool
    glyph_count: int
    font: FontRef | None
    tf_size: float
    tm: Matrix
    tc: float
    tw: float
    tz: float
    tl: float
    ts: float
    tr: int
    fill: ColorValue
    stroke: ColorValue
    fill_alpha: float
    stroke_alpha: float
    in_text_object: bool
    # -- edit addressing (M3): where this show lives so it can be rewritten --
    stream_id: str | None = None  # None = page content, else form XObject objgen
    instr: int = -1  # instruction index inside that stream's instruction list
    tf_instr: int = -1  # index of the Tf that selected the font (-1 = none)
    ctm: Matrix = IDENTITY  # graphics matrix when the show ran (fallback path)

    @property
    def size(self) -> float:
        """Effective glyph size in user space (Tf size x text matrix scale)."""
        return self.tf_size * mat_scale(self.tm)


@dataclass(frozen=True)
class PaintRecord:
    """One painted path (fill and/or stroke) in PDF user space."""

    seq: int
    kind: str  # fill | eofill | stroke | fillstroke | eofillstroke
    bbox: Rect
    fill: ColorValue | None
    stroke: ColorValue | None
    line_width: float | None  # effective width in user space
    fill_alpha: float
    stroke_alpha: float


@dataclass
class ParseResult:
    """Everything the parser saw on one page."""

    shows: list[ShowEvent] = field(default_factory=list)
    paints: list[PaintRecord] = field(default_factory=list)
    fonts: dict[str, FontRef] = field(default_factory=dict)
    font_dicts: dict[str, pikepdf.Dictionary] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# operand helpers
# --------------------------------------------------------------------------- #
def _to_float(obj: object) -> float | None:
    if isinstance(obj, bool):
        return None
    if isinstance(obj, (int, float, Decimal)):
        return float(obj)
    return None


def _mat6(operands: Sequence[object]) -> Matrix | None:
    if len(operands) < 6:
        return None
    values = [_to_float(v) for v in operands[:6]]
    if any(v is None for v in values):
        return None
    return tuple(values)  # type: ignore[return-value]


def _string_bytes(obj: object) -> bytes | None:
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj)
    # pikepdf.String behaves like a binary object; bytes() is the supported
    # conversion (attribute access is intercepted as PDF key lookup).
    try:
        return bytes(obj)  # type: ignore[call-overload]
    except Exception:
        return None


def _name(obj: object) -> str | None:
    if isinstance(obj, pikepdf.Name):
        return str(obj).lstrip("/")
    return None


def _cmap_code_lengths(data: bytes) -> tuple[int, ...]:
    """Code byte lengths declared by a embedded CMap's codespacerange."""
    text = data.decode("latin-1", errors="replace")
    lengths: set[int] = set()
    for block in _CODESPACE_RE.findall(text):
        for low, high in _HEX_RANGE_RE.findall(block):
            lengths.add(len(low) // 2)
            lengths.add(len(high) // 2)
    return tuple(sorted(lengths)) or (2,)


def count_glyphs(data: bytes, font: FontRef | None) -> int:
    """Number of glyphs a byte string shows with ``font``."""
    if font is None or font.subtype != "Type0":
        return len(data)
    lengths = font.code_lengths or (2,)
    if len(lengths) == 1 and lengths[0] == 1:
        return len(data)
    index = 0
    count = 0
    ordered = sorted(lengths, reverse=True)
    while index < len(data):
        for length in ordered:
            if index + length <= len(data):
                index += length
                count += 1
                break
        else:
            index += 1
    return count


# --------------------------------------------------------------------------- #
# internal state
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _TextState:
    font_name: str = ""
    size: float = 0.0
    tc: float = 0.0
    tw: float = 0.0
    tz: float = 100.0
    tl: float = 0.0
    ts: float = 0.0
    tr: int = 0


@dataclass(frozen=True)
class _GState:
    ctm: Matrix = IDENTITY
    fill: ColorValue = BLACK
    stroke: ColorValue = BLACK
    fill_cs: tuple[str, int | None, str | None] = ("DeviceGray", None, None)
    stroke_cs: tuple[str, int | None, str | None] = ("DeviceGray", None, None)
    fill_alpha: float = 1.0
    stroke_alpha: float = 1.0
    line_width: float = 1.0
    text: _TextState = field(default_factory=_TextState)


@dataclass
class _Frame:
    resources: pikepdf.Dictionary | None
    form_objgen: str | None = None


# --------------------------------------------------------------------------- #
# the parser
# --------------------------------------------------------------------------- #
class ContentStreamParser:
    """Walks one page's content streams and records shows + paints."""

    def __init__(self, pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
        del pdf  # kept for future use (resource resolution across the file)
        self._page = page
        self._result = ParseResult()
        self._seq = 0
        self._warned: set[str] = set()

        self._gstack: list[_GState] = []
        self._g = _GState()
        self._in_text = False
        self._tm: Matrix = IDENTITY
        self._tlm: Matrix = IDENTITY

        self._frames: list[_Frame] = []
        self._form_stack: list[str] = []

        self._path: list[Point] = []
        self._cp: Point | None = None

        # edit addressing: current instruction index / stream / Tf index
        self._stream_id: str | None = None
        self._instr = -1
        self._tf_instr = -1

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def parse(self) -> ParseResult:
        """Parse the page and return the recorded events."""
        contents = self._page.get("/Contents")
        if contents is None:
            return self._result
        resources = self._page.get("/Resources")
        self._frames.append(
            _Frame(
                resources=resources if isinstance(resources, pikepdf.Dictionary) else None
            )
        )
        try:
            self._run(pikepdf.parse_content_stream(self._page))
        except pikepdf.PdfError as exc:
            self._warn(f"content stream parse failed: {exc}")
        finally:
            self._frames.pop()
        return self._result

    # ------------------------------------------------------------------ #
    # warnings
    # ------------------------------------------------------------------ #
    def _warn(self, message: str) -> None:
        if message not in self._warned:
            self._warned.add(message)
            self._result.warnings.append(message)

    # ------------------------------------------------------------------ #
    # resource lookup
    # ------------------------------------------------------------------ #
    def _resource(self, category: str, name: str) -> object | None:
        """Resolve ``name`` in ``category`` walking the frame stack upward."""
        key = "/" + name
        for frame in reversed(self._frames):
            resources = frame.resources
            if resources is None:
                continue
            sub = resources.get("/" + category)
            if isinstance(sub, pikepdf.Dictionary) and key in sub:
                return sub.get(key)
        return None

    # ------------------------------------------------------------------ #
    # operator dispatch
    # ------------------------------------------------------------------ #
    def _run(self, ops: Iterable[Any]) -> None:
        for index, instruction in enumerate(ops):
            operands, operator = instruction
            op = str(operator)
            self._instr = index
            if op == "INLINE IMAGE":
                self._path.clear()
                self._cp = None
                continue
            self._dispatch(op, operands)

    def _dispatch(self, op: str, operands: Sequence[object]) -> None:
        # --- graphics state ------------------------------------------------
        if op == "q":
            if len(self._gstack) < _MAX_GSTATE_DEPTH:
                self._gstack.append(self._g)
            else:
                self._warn("q stack overflow (state clamp)")
            return
        if op == "Q":
            if self._gstack:
                self._g = self._gstack.pop()
            else:
                self._warn("Q without matching q")
            return
        if op == "cm":
            matrix = _mat6(operands)
            if matrix is not None and not mat_is_identity(matrix):
                self._g = replace(self._g, ctm=mat_mul(matrix, self._g.ctm))
            return

        # --- colour --------------------------------------------------------
        if op in ("g", "G", "rg", "RG", "k", "K"):
            self._direct_colour(op, operands)
            return
        if op == "cs":
            self._set_colour_space("fill", operands)
            return
        if op == "CS":
            self._set_colour_space("stroke", operands)
            return
        if op in ("sc", "scn"):
            self._set_components("fill", operands)
            return
        if op in ("SC", "SCN"):
            self._set_components("stroke", operands)
            return
        if op == "gs":
            self._ext_gstate(operands)
            return
        if op == "w":
            value = _to_float(operands[0]) if operands else None
            if value is not None:
                self._g = replace(self._g, line_width=value)
            return

        # --- path construction / painting ----------------------------------
        if op in ("m", "l", "c", "v", "y", "re", "h"):
            self._path_op(op, operands)
            return
        if op in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*"):
            self._paint(op)
            return
        if op == "n":
            self._path.clear()
            self._cp = None
            return
        if op in ("W", "W*"):
            return  # clip affects the next 'n'; we only track painted paths

        # --- text state ------------------------------------------------------
        if op == "BT":
            self._in_text = True
            self._tm = IDENTITY
            self._tlm = IDENTITY
            return
        if op == "ET":
            self._in_text = False
            return
        if op == "Tf":
            if len(operands) >= 2:
                name = _name(operands[0])
                size = _to_float(operands[1])
                text = replace(
                    self._g.text,
                    font_name=name or "",
                    size=size if size is not None else 0.0,
                )
                self._g = replace(self._g, text=text)
                self._tf_instr = self._instr
            return
        if op == "Tc":
            self._set_text_param("tc", operands)
            return
        if op == "Tw":
            self._set_text_param("tw", operands)
            return
        if op == "Tz":
            self._set_text_param("tz", operands)
            return
        if op == "TL":
            self._set_text_param("tl", operands)
            return
        if op == "Ts":
            self._set_text_param("ts", operands)
            return
        if op == "Tr":
            value = _to_float(operands[0]) if operands else None
            if value is not None:
                text = replace(self._g.text, tr=int(value))
                self._g = replace(self._g, text=text)
            return

        # --- text positioning ------------------------------------------------
        if op == "Td" and len(operands) >= 2:
            tx = _to_float(operands[0])
            ty = _to_float(operands[1])
            if tx is not None and ty is not None:
                self._td(tx, ty)
            return
        if op == "TD" and len(operands) >= 2:
            tx = _to_float(operands[0])
            ty = _to_float(operands[1])
            if tx is not None and ty is not None:
                text = replace(self._g.text, tl=-ty)
                self._g = replace(self._g, text=text)
                self._td(tx, ty)
            return
        if op == "Tm":
            matrix = _mat6(operands)
            if matrix is not None:
                self._tm = matrix
                self._tlm = matrix
            return
        if op == "T*":
            self._td(0.0, -self._g.text.tl)
            return

        # --- show ------------------------------------------------------------
        if op == "Tj":
            if operands:
                self._show([operands[0]])
            return
        if op == "TJ":
            if operands and isinstance(operands[0], pikepdf.Array):
                self._show(list(operands[0]))
            return
        if op == "'":
            self._td(0.0, -self._g.text.tl)
            if operands:
                self._show([operands[0]])
            return
        if op == '"' and len(operands) >= 3:
            word = _to_float(operands[0])
            char = _to_float(operands[1])
            if word is not None:
                self._g = replace(self._g, text=replace(self._g.text, tw=word))
            if char is not None:
                self._g = replace(self._g, text=replace(self._g.text, tc=char))
            self._td(0.0, -self._g.text.tl)
            self._show([operands[2]])
            return

        # --- xobjects ---------------------------------------------------------
        if op == "Do":
            self._do_xobject(operands)
            return

        # everything else (marked content, dash patterns, shadings, ...) is
        # intentionally ignored - it cannot change text style.

    # ------------------------------------------------------------------ #
    # graphics state helpers
    # ------------------------------------------------------------------ #
    def _direct_colour(self, op: str, operands: Sequence[object]) -> None:
        values = tuple(v for v in (_to_float(x) for x in operands) if v is not None)
        if op in ("g", "G"):
            space, count = "DeviceGray", 1
        elif op in ("rg", "RG"):
            space, count = "DeviceRGB", 3
        else:
            space, count = "DeviceCMYK", 4
        if len(values) < count:
            return
        colour = ColorValue(space, values[:count])
        cs = (space, None, None)
        if op in ("g", "rg", "k"):
            self._g = replace(self._g, fill=colour, fill_cs=cs)
        else:
            self._g = replace(self._g, stroke=colour, stroke_cs=cs)

    def _set_colour_space(self, which: str, operands: Sequence[object]) -> None:
        name = _name(operands[0]) if operands else None
        if name is None:
            return
        space, n, base = self._resolve_colour_space(name)
        template = (space, n, base)
        if which == "fill":
            self._g = replace(self._g, fill_cs=template)
        else:
            self._g = replace(self._g, stroke_cs=template)

    def _resolve_colour_space(self, name: str) -> tuple[str, int | None, str | None]:
        obj = self._resource("ColorSpace", name)
        if obj is None:
            return (name, None, None)
        if isinstance(obj, pikepdf.Name):
            return (str(obj).lstrip("/"), None, None)
        if isinstance(obj, pikepdf.Array) and len(obj) >= 1:
            head = _name(obj[0])
            if head == "ICCBased":
                n = None
                if len(obj) >= 2 and isinstance(obj[1], pikepdf.Stream):
                    raw = obj[1].get("/N")
                    value = _to_float(raw) if raw is not None else None
                    n = int(value) if value is not None else None
                return ("ICCBased", n, None)
            if head == "Separation" and len(obj) >= 3:
                base = self._base_space(obj[2])
                return ("Separation", 1, base)
            if head == "DeviceN" and len(obj) >= 2:
                colorants = obj[1]
                count = len(colorants) if isinstance(colorants, (pikepdf.Array, list)) else None
                base = self._base_space(obj[2]) if len(obj) >= 3 else None
                return ("DeviceN", count, base)
            if head == "Indexed" and len(obj) >= 2:
                return ("Indexed", 1, self._base_space(obj[1]))
            if head == "Pattern":
                base = self._base_space(obj[1]) if len(obj) >= 2 else None
                return ("Pattern", None, base)
            if head is not None:
                return (head, None, None)
        return (name, None, None)

    def _base_space(self, obj: object) -> str | None:
        if isinstance(obj, pikepdf.Name):
            return str(obj).lstrip("/")
        return None

    def _set_components(self, which: str, operands: Sequence[object]) -> None:
        if which == "fill":
            space, n, base = self._g.fill_cs
        else:
            space, n, base = self._g.stroke_cs
        pattern: str | None = None
        rest: Sequence[object] = operands
        if space == "Pattern":
            first = _name(operands[0]) if operands else None
            if first is not None:
                pattern = first
                rest = list(operands[1:])
        values = tuple(v for v in (_to_float(x) for x in rest) if v is not None)
        colour = ColorValue(space, values, pattern, n, base)
        if which == "fill":
            self._g = replace(self._g, fill=colour)
        else:
            self._g = replace(self._g, stroke=colour)

    def _ext_gstate(self, operands: Sequence[object]) -> None:
        name = _name(operands[0]) if operands else None
        if name is None:
            return
        obj = self._resource("ExtGState", name)
        if not isinstance(obj, pikepdf.Dictionary):
            return
        changes: dict[str, object] = {}
        for key, attr in (("/ca", "fill_alpha"), ("/CA", "stroke_alpha"), ("/LW", "line_width")):
            raw = obj.get(key)
            value = _to_float(raw) if raw is not None else None
            if value is not None:
                changes[attr] = value
        if changes:
            self._g = replace(self._g, **changes)  # type: ignore[arg-type]

    def _set_text_param(self, attr: str, operands: Sequence[object]) -> None:
        value = _to_float(operands[0]) if operands else None
        if value is None:
            return
        text = replace(self._g.text, **{attr: value})  # type: ignore[arg-type]
        self._g = replace(self._g, text=text)

    # ------------------------------------------------------------------ #
    # path construction / painting
    # ------------------------------------------------------------------ #
    def _user_point(self, x: float, y: float) -> Point:
        return mat_apply(self._g.ctm, x, y)

    def _path_op(self, op: str, operands: Sequence[object]) -> None:
        numbers = [v for v in (_to_float(x) for x in operands) if v is not None]
        if op == "h":
            return
        if op == "m" and len(numbers) >= 2:
            point = self._user_point(numbers[0], numbers[1])
            self._path.append(point)
            self._cp = point
            return
        if op == "l" and len(numbers) >= 2 and self._cp is not None:
            point = self._user_point(numbers[0], numbers[1])
            self._path.append(point)
            self._cp = point
            return
        if op == "re" and len(numbers) >= 4:
            x, y, w, h = numbers[:4]
            corners = ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
            self._path.extend(self._user_point(cx, cy) for cx, cy in corners)
            self._cp = (x, y)
            return
        if op == "c" and len(numbers) >= 6:
            for i in range(0, 6, 2):
                self._path.append(self._user_point(numbers[i], numbers[i + 1]))
            self._cp = (numbers[4], numbers[5])
            return
        if op == "v" and len(numbers) >= 4 and self._cp is not None:
            self._path.append(self._user_point(*self._cp))
            for i in range(0, 4, 2):
                self._path.append(self._user_point(numbers[i], numbers[i + 1]))
            self._cp = (numbers[2], numbers[3])
            return
        if op == "y" and len(numbers) >= 4:
            for i in range(0, 4, 2):
                self._path.append(self._user_point(numbers[i], numbers[i + 1]))
            self._path.append(self._user_point(numbers[2], numbers[3]))
            self._cp = (numbers[2], numbers[3])
            return

    def _paint(self, op: str) -> None:
        points = self._path
        self._path = []
        self._cp = None
        if not points:
            return
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        fills = op in ("f", "F", "f*", "B", "B*")
        strokes = op in ("S", "s", "B", "B*", "b", "b*")
        if op in ("f*", "B*", "b*") and fills:
            kind = "eofillstroke" if strokes else "eofill"
        elif fills and strokes:
            kind = "fillstroke"
        elif fills:
            kind = "fill"
        else:
            kind = "stroke"
        fill = self._g.fill if fills else None
        stroke = self._g.stroke if strokes else None
        line_width = (
            self._g.line_width * mat_scale(self._g.ctm) if strokes else None
        )
        record = PaintRecord(
            seq=self._seq,
            kind=kind,
            bbox=bbox,
            fill=fill,
            stroke=stroke,
            line_width=line_width,
            fill_alpha=self._g.fill_alpha,
            stroke_alpha=self._g.stroke_alpha,
        )
        self._seq += 1
        self._result.paints.append(record)

    # ------------------------------------------------------------------ #
    # text positioning / showing
    # ------------------------------------------------------------------ #
    def _td(self, tx: float, ty: float) -> None:
        translate: Matrix = (1.0, 0.0, 0.0, 1.0, tx, ty)
        self._tlm = mat_mul(translate, self._tlm)
        self._tm = self._tlm

    def _text_origin(self) -> Point:
        x, y = mat_apply(self._tm, 0.0, self._g.text.ts)
        return mat_apply(self._g.ctm, x, y)

    def _show(self, items: Sequence[object]) -> None:
        strings = [data for data in (_string_bytes(x) for x in items) if data is not None]
        if not strings:
            return
        origin = self._text_origin()
        known = self._in_text
        if not known:
            self._warn("show text operation outside BT/ET")
        font = self._resolve_font(self._g.text.font_name) if self._g.text.font_name else None
        glyph_count = sum(count_glyphs(data, font) for data in strings)
        if glyph_count == 0:
            return
        text = self._g.text
        event = ShowEvent(
            seq=self._seq,
            origin=origin,
            origin_known=known,
            glyph_count=glyph_count,
            font=font,
            tf_size=text.size,
            tm=self._tm,
            tc=text.tc,
            tw=text.tw,
            tz=text.tz,
            tl=text.tl,
            ts=text.ts,
            tr=text.tr,
            fill=self._g.fill,
            stroke=self._g.stroke,
            fill_alpha=self._g.fill_alpha,
            stroke_alpha=self._g.stroke_alpha,
            in_text_object=self._in_text,
            stream_id=self._stream_id,
            instr=self._instr,
            tf_instr=self._tf_instr,
            ctm=self._g.ctm,
        )
        self._seq += 1
        self._result.shows.append(event)

    # ------------------------------------------------------------------ #
    # fonts
    # ------------------------------------------------------------------ #
    def _resolve_font(self, name: str) -> FontRef | None:
        obj = self._resource("Font", name)
        if obj is None:
            self._warn(f"font resource not found: {name}")
            return None
        if not isinstance(obj, pikepdf.Dictionary):
            return None
        objgen = _objgen(obj)
        cached = self._result.fonts.get(objgen)
        if cached is not None:
            return cached
        try:
            ref = _build_font_ref(name, objgen, obj)
        except Exception as exc:  # pragma: no cover - malformed font dict
            self._warn(f"font dictionary unreadable ({objgen}): {exc}")
            return None
        self._result.fonts[objgen] = ref
        self._result.font_dicts[objgen] = obj
        return ref

    # ------------------------------------------------------------------ #
    # xobjects
    # ------------------------------------------------------------------ #
    def _do_xobject(self, operands: Sequence[object]) -> None:
        name = _name(operands[0]) if operands else None
        if name is None:
            return
        obj = self._resource("XObject", name)
        if not isinstance(obj, pikepdf.Dictionary):
            self._warn(f"XObject resource not found: {name}")
            return
        subtype = str(obj.get("/Subtype") or "")
        if subtype != "/Form":
            return  # images, PS XObjects and tiling patterns carry no text style
        objgen = _objgen(obj)
        if objgen in self._form_stack:
            self._warn(f"cyclic form XObject reference: {objgen}")
            return
        if len(self._form_stack) >= _MAX_FORM_DEPTH:
            self._warn("form XObject nesting too deep")
            return
        if "/Resources" not in obj and not any(
            frame.resources is not None for frame in self._frames
        ):
            self._warn(f"form XObject without resources: {objgen}")

        self._form_stack.append(objgen)
        self._gstack.append(self._g)
        saved_stream = self._stream_id
        saved_tf_instr = self._tf_instr
        saved_instr = self._instr
        self._stream_id = objgen
        self._tf_instr = -1
        self._instr = -1
        matrix = obj.get("/Matrix")
        if isinstance(matrix, pikepdf.Array):
            parsed = _mat6(list(matrix))
            if parsed is not None and not mat_is_identity(parsed):
                self._g = replace(self._g, ctm=mat_mul(parsed, self._g.ctm))
        resources = obj.get("/Resources")
        self._frames.append(
            _Frame(
                resources=resources if isinstance(resources, pikepdf.Dictionary) else None,
                form_objgen=objgen,
            )
        )
        try:
            self._run(pikepdf.parse_content_stream(obj))
        except pikepdf.PdfError as exc:
            self._warn(f"form XObject content unreadable ({objgen}): {exc}")
        finally:
            self._frames.pop()
            self._g = self._gstack.pop()
            self._form_stack.pop()
            self._stream_id = saved_stream
            self._tf_instr = saved_tf_instr
            self._instr = saved_instr


# --------------------------------------------------------------------------- #
# font dictionary parsing
# --------------------------------------------------------------------------- #
def _objgen(obj: pikepdf.Object) -> str:
    objnum, gennum = obj.objgen
    return f"{objnum} {gennum}"


def _build_font_ref(res_name: str, objgen: str, font: pikepdf.Dictionary) -> FontRef:
    subtype = str(font.get("/Subtype") or "").lstrip("/")
    base_font = str(font.get("/BaseFont") or "").lstrip("/")
    encoding_obj = font.get("/Encoding")
    encoding = str(encoding_obj).lstrip("/") if isinstance(encoding_obj, pikepdf.Name) else ""

    descriptor: pikepdf.Dictionary | None = None
    flags: int | None = None
    italic_angle: float | None = None
    embedded = False
    differences: tuple[tuple[int, str], ...] = ()
    code_lengths: tuple[int, ...] = (1,)

    if subtype == "Type0":
        descendants = font.get("/DescendantFonts")
        target: object = None
        if isinstance(descendants, pikepdf.Array) and len(descendants) >= 1:
            target = descendants[0]
        if isinstance(target, pikepdf.Dictionary):
            descriptor = _descriptor_of(target)
            flags = _flags_of(descriptor)
            italic_angle = _italic_angle_of(descriptor)
            embedded = _program_of(descriptor) is not None
        code_lengths = _type0_code_lengths(encoding_obj)
        if isinstance(encoding_obj, pikepdf.Stream):
            try:
                code_lengths = _cmap_code_lengths(bytes(encoding_obj.read_bytes()))
            except Exception:  # pragma: no cover - unreadable CMap
                code_lengths = (2,)
    elif subtype == "Type3":
        # glyph procedures are part of the file itself - always "embedded"
        embedded = True
        descriptor = _descriptor_of(font)
        flags = _flags_of(descriptor)
        differences = _differences_of(encoding_obj)
    else:
        descriptor = _descriptor_of(font)
        flags = _flags_of(descriptor)
        italic_angle = _italic_angle_of(descriptor)
        embedded = _program_of(descriptor) is not None
        differences = _differences_of(encoding_obj)

    return FontRef(
        res_name=res_name,
        objgen=objgen,
        subtype=subtype or "Unknown",
        base_font=base_font,
        embedded=embedded,
        descriptor_flags=flags,
        italic_angle=italic_angle,
        code_lengths=code_lengths,
        differences=differences,
        encoding=encoding,
    )


def _descriptor_of(font: object) -> pikepdf.Dictionary | None:
    if not isinstance(font, pikepdf.Dictionary):
        return None
    descriptor = font.get("/FontDescriptor")
    return descriptor if isinstance(descriptor, pikepdf.Dictionary) else None


def _flags_of(descriptor: pikepdf.Dictionary | None) -> int | None:
    if descriptor is None:
        return None
    raw = descriptor.get("/Flags")
    value = _to_float(raw) if raw is not None else None
    return int(value) if value is not None else None


def _italic_angle_of(descriptor: pikepdf.Dictionary | None) -> float | None:
    if descriptor is None:
        return None
    raw = descriptor.get("/ItalicAngle")
    return _to_float(raw) if raw is not None else None


def _program_of(descriptor: pikepdf.Dictionary | None) -> str | None:
    if descriptor is None:
        return None
    for key in ("/FontFile", "/FontFile2", "/FontFile3"):
        if descriptor.get(key) is not None:
            return key
    return None


def _type0_code_lengths(encoding: object) -> tuple[int, ...]:
    if isinstance(encoding, pikepdf.Stream):
        try:
            return _cmap_code_lengths(bytes(encoding.read_bytes()))
        except Exception:  # pragma: no cover - unreadable CMap
            return (2,)
    name = _name(encoding)
    if name is not None and (name.startswith("Identity-")):
        return (2,)
    # predefined CMaps (UniGB-UCS2-H, ...) are two-byte in practice
    return (2,)


def _differences_of(encoding: object) -> tuple[tuple[int, str], ...]:
    if not isinstance(encoding, pikepdf.Dictionary):
        return ()
    differences = encoding.get("/Differences")
    if not isinstance(differences, pikepdf.Array):
        return ()
    pairs: list[tuple[int, str]] = []
    code = 0
    for item in differences:
        number = _to_float(item)
        if number is not None:
            code = int(number)
            continue
        glyph_name = _name(item)
        if glyph_name is not None:
            pairs.append((code, glyph_name))
            code += 1
    return tuple(pairs)


# --------------------------------------------------------------------------- #
# module-level entry point
# --------------------------------------------------------------------------- #
def parse_page_content(pdf: pikepdf.Pdf, page: pikepdf.Page) -> ParseResult:
    """Parse ``page`` (and its form XObjects) and record shows + paints."""
    return ContentStreamParser(pdf, page).parse()
