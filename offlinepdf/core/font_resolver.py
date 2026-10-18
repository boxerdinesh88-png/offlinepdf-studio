"""Offline font resolution - master plan section 8, steps 1-7.

Given the original PDF font and the text that is about to be written, decide
which font file to use, with the confidence and the runner-up alternatives the
Edit-mode UI shows as a status badge.

The order is fixed by the specification:

1. **reuse** the original font resource when it already has every needed
   glyph (keeps the original encoding/CID mapping - identical output).
2. **same-family**: when the embedded font is a subset missing glyphs, look for
   the same family in the bundled/system index (offline) and confirm it by
   comparing advance widths (must match within 1-2%).
3. **nearest**: otherwise score class (serif/sans/mono/script), weight, width,
   slant, x-height/cap-height and average advance.  Confidence below 0.8 means
   "approximate match" and the top-3 alternatives are attached.
4. **synthetic**: bold (stroke) / italic (skew) only as a last resort and
   marked as synthetic.
5. **script**: mixed scripts are split into runs and rendered with the bundled
   Noto family for that script (Latin, Greek, Cyrillic, Arabic, Hebrew,
   Devanagari, CJK), keeping size and colour.
6. the caller embeds only the needed subset of the chosen font and keeps a
   ToUnicode map (``FontDecision.subset`` + ``FontDecision.record``).
7. decisions are cached per document (one :class:`FontResolver` per session).
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass

from fontTools import unicodedata as ft_unicodedata
from fontTools.ttLib import TTFont, TTLibError

from offlinepdf.core.font_index import (
    REFERENCE_SAMPLE,
    FontIndex,
    FontRecord,
    default_index,
)
from offlinepdf.core.font_program import (
    FontProgram,
    advance_widths,
    codepoint_coverage,
)

__all__ = [
    "CONFIDENCE_APPROX",
    "STRATEGY_NEAREST",
    "STRATEGY_REUSE",
    "STRATEGY_SAME_FAMILY",
    "STRATEGY_SCRIPT",
    "STRATEGY_SYNTHETIC",
    "Candidate",
    "FontDecision",
    "FontRequest",
    "FontResolver",
    "ResolverStats",
    "ScriptRun",
]

STRATEGY_REUSE = "reuse"
STRATEGY_SAME_FAMILY = "same-family"
STRATEGY_NEAREST = "nearest"
STRATEGY_SYNTHETIC = "synthetic"
STRATEGY_SCRIPT = "script"

#: Below this confidence the badge reads "approximate match (N alternatives)".
CONFIDENCE_APPROX = 0.8

# Metric-compatible pairs: requested family -> bundled/system families whose
# advance widths are known to match (Arial -> Liberation Sans, Calibri ->
# Carlito, ...).  Step 2 still confirms them against the actual glyph widths.
_METRIC_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "helvetica": ("arial", "liberationsans", "arimo"),
    "helveticaneue": ("arial", "liberationsans"),
    "arial": ("liberationsans", "arimo"),
    "arialmt": ("liberationsans", "arimo"),
    "times": ("timesnewroman", "liberationserif", "tinos"),
    "timesnewroman": ("liberationserif", "tinos"),
    "timesroman": ("liberationserif", "tinos"),
    "timesnewromanps": ("liberationserif", "tinos"),
    "courier": ("couriernew", "liberationmono", "cousine"),
    "couriernew": ("liberationmono", "cousine"),
    "calibri": ("carlito",),
    "calibri light": ("carlito",),
    "cambria": ("caladea",),
    "consolas": ("liberationmono", "dejavusansmono"),
    "georgia": ("liberationserif", "dejavuserif"),
    "verdana": ("dejavusans", "liberationsans"),
    "tahoma": ("dejavusans", "liberationsans"),
}

# Step 5: script code (ISO 15924) -> bundled Noto family (font index name).
_SCRIPT_FAMILIES: dict[str, str] = {
    "Arab": "Noto Sans Arabic",
    "Hebr": "Noto Sans Hebrew",
    "Deva": "Noto Sans Devanagari",
    "Hani": "Noto Sans SC",
    "Hira": "Noto Sans SC",
    "Kana": "Noto Sans SC",
    "Hang": "Noto Sans SC",
    "Bopo": "Noto Sans SC",
}

#: Scripts that keep the base (step 2/3) family.
_BASE_SCRIPTS = frozenset({"Latn", "Grek", "Cyrl"})

#: Scripts with no dedicated bundled family (joined to the previous run).
_NEUTRAL_SCRIPTS = frozenset({"Zyyy", "Zinh", "Zzzz", "Zmth", "Zsym"})

_SERIF_HINTS = ("serif", "times", "georgia", "garamond", "palatino", "book")
_SANS_HINTS = (
    "sans",
    "arial",
    "helvetica",
    "calibri",
    "carlito",
    "verdana",
    "tahoma",
    "segoe",
    "trebuchet",
    "impact",
    "simsun",
    "msyh",
)
_MONO_HINTS = ("mono", "courier", "consol", "menlo", "monaco")

_SAME_FAMILY_CONFIDENCE = 0.95
_MAX_TOP_FAMILIES = 8


def _norm(name: str) -> str:
    """Family name key: lower-case, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _strip_subset(name: str) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", name or "")


def _ratio_score(value: float, reference: float, tolerance: float) -> float:
    if reference <= 0 or value <= 0:
        return 0.0
    delta = abs(value / reference - 1.0)
    return max(0.0, 1.0 - min(delta, tolerance) / tolerance)


def _script_label(code: str) -> str:
    """Human readable script name (``Deva`` -> ``Devanagari``)."""
    if not code:
        return "unknown"
    try:
        return ft_unicodedata.script_name(code)
    except Exception:
        return code


def _class_of(family: str, monospace: bool) -> str | None:
    """serif / sans / mono classification from a family name (None if unsure)."""
    key = _norm(family)
    if monospace or any(hint in key for hint in _MONO_HINTS):
        return "mono"
    if any(hint in key for hint in _SERIF_HINTS):
        return "serif"
    if any(hint in key for hint in _SANS_HINTS):
        return "sans"
    return None


# --------------------------------------------------------------------------- #
# request / decision types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FontRequest:
    """What the caller wants to write, plus what the original font is."""

    text: str
    program: FontProgram
    font_bytes: bytes | None = None
    font_kind: str = ""
    weight: int | None = None
    italic: bool | None = None
    cache_key: str = ""

    @property
    def target_weight(self) -> int:
        return self.weight if self.weight is not None else self.program.weight

    @property
    def target_italic(self) -> bool:
        return self.italic if self.italic is not None else self.program.italic

    @property
    def codepoints(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(ord(char) for char in self.text))


@dataclass(frozen=True)
class Candidate:
    """One scored font face (chosen, or listed as an alternative)."""

    record: FontRecord
    score: float
    reason: str

    @property
    def family(self) -> str:
        return self.record.family

    @property
    def subfamily(self) -> str:
        return self.record.subfamily

    @property
    def path(self) -> str:
        return self.record.path

    @property
    def face(self) -> int:
        return self.record.face

    @property
    def source(self) -> str:
        return self.record.source

    @property
    def display(self) -> str:
        return f"{self.record.family} {self.record.subfamily}".strip()

    def to_dict(self) -> dict[str, object]:
        return {
            "family": self.record.family,
            "subfamily": self.record.subfamily,
            "display": self.display,
            "path": self.record.path,
            "source": self.record.source,
            "score": round(self.score, 3),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ScriptRun:
    """One script run of the edited text and the family chosen for it."""

    script: str
    text: str
    family: str
    path: str | None
    face: int

    def to_dict(self) -> dict[str, object]:
        return {
            "script": self.script,
            "text": self.text,
            "family": self.family,
            "path": self.path,
        }


@dataclass
class FontDecision:
    """The answer to "which font do we write with?" (all of section 8)."""

    requested: str
    strategy: str
    family: str
    confidence: float
    record: FontRecord | None
    alternatives: tuple[Candidate, ...] = ()
    runs: tuple[ScriptRun, ...] = ()
    trace: tuple[str, ...] = ()
    synthetic: tuple[str, ...] = ()
    metric_match: bool = False
    coverage_ok: bool = True
    subset: bool = True

    @property
    def approximate(self) -> bool:
        """True when the badge must read "approximate match"."""
        return self.confidence < CONFIDENCE_APPROX

    @property
    def badge(self) -> str:
        """Status badge text for the Edit-mode UI."""
        if self.strategy == STRATEGY_REUSE:
            return "original font reused"
        if self.approximate and self.alternatives:
            return f"approximate match ({len(self.alternatives)} alternatives)"
        if self.approximate:
            return "approximate match"
        return "match found"

    @property
    def path(self) -> str | None:
        return self.record.path if self.record is not None else None

    @property
    def face(self) -> int:
        return self.record.face if self.record is not None else 0

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "strategy": self.strategy,
            "family": self.family,
            "confidence": round(self.confidence, 3),
            "approximate": self.approximate,
            "badge": self.badge,
            "path": self.path,
            "face": self.face,
            "synthetic": list(self.synthetic),
            "metric_match": self.metric_match,
            "coverage_ok": self.coverage_ok,
            "subset": self.subset,
            "alternatives": [alt.to_dict() for alt in self.alternatives],
            "runs": [run.to_dict() for run in self.runs],
            "trace": list(self.trace),
        }


@dataclass
class ResolverStats:
    """Counters used by tests and the selftest."""

    requests: int = 0
    cache_hits: int = 0
    reused: int = 0
    same_family: int = 0
    nearest: int = 0
    synthetic: int = 0
    script: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "reused": self.reused,
            "same_family": self.same_family,
            "nearest": self.nearest,
            "synthetic": self.synthetic,
            "script": self.script,
        }


# --------------------------------------------------------------------------- #
# font fingerprints (the metrics step 3 scores against)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _FP:
    family_class: str | None
    monospace: bool
    avg_advance: float  # per 1000 em over the reference sample
    x_ratio: float  # x-height / em
    cap_ratio: float  # cap-height / em
    coverage: frozenset[int] | None


def _distinct_families(
    candidates: list[Candidate],
    *,
    exclude: FontRecord | None = None,
    chosen: Candidate | None = None,
    limit: int = 3,
) -> list[Candidate]:
    """Top ``limit`` candidates, one face per family, minus the pick's family."""
    out: list[Candidate] = []
    seen: set[str] = set()
    if exclude is not None:
        seen.add(_norm(exclude.family))
    elif chosen is not None:
        seen.add(_norm(chosen.family))
    for candidate in candidates:
        if exclude is not None and candidate.path == exclude.path:
            continue
        if chosen is not None and (
            candidate.path == chosen.path and candidate.face == chosen.face
        ):
            continue
        key = _norm(candidate.family)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
        if len(out) >= limit:
            break
    return out


def _fingerprint(record: FontRecord) -> _FP:
    """Metric fingerprint of an indexed face - no file access (fields cached)."""
    return _FP(
        family_class=_class_of(record.family, record.monospace),
        monospace=record.monospace,
        avg_advance=record.avg_advance,
        x_ratio=record.x_ratio,
        cap_ratio=record.cap_ratio,
        coverage=None,
    )


def _coverage_of_file(path: str, face: int) -> frozenset[int] | None:
    """Unicode coverage of a candidate file (opened lazily, cached by caller)."""
    try:
        font = TTFont(path, fontNumber=face, lazy=True)
        cmap = font.getBestCmap()
        font.close()
    except (OSError, TTLibError, KeyError, ValueError, TypeError):
        return None
    return frozenset(cmap) if cmap else None


def _fp_from_program(
    data: bytes, kind: str, family: str, monospace: bool
) -> _FP | None:
    """Fingerprint of the *original* embedded program (the metric reference)."""
    if kind == "type1":
        return None
    try:
        font = TTFont(io.BytesIO(data), lazy=True)
        cmap = font.getBestCmap() or {}
        upm = int(font["head"].unitsPerEm or 1000)
        metrics = font["hmtx"].metrics
        widths: list[float] = []
        for char in REFERENCE_SAMPLE:
            glyph = cmap.get(ord(char))
            if glyph is not None and glyph in metrics:
                widths.append(metrics[glyph][0] * 1000.0 / upm)
        avg = sum(widths) / len(widths) if widths else 0.0
        os2 = font.get("OS/2")
        x_ratio = cap_ratio = 0.0
        if os2 is not None:
            x_ratio = float(getattr(os2, "sxHeight", 0) or 0) / upm
            cap_ratio = float(getattr(os2, "sCapHeight", 0) or 0) / upm
        mono = bool(font.get("post") is not None and font["post"].isFixedPitch)
        font.close()
    except (OSError, TTLibError, KeyError, ValueError, TypeError):
        return None
    return _FP(
        _class_of(family, mono or monospace),
        mono or monospace,
        avg,
        x_ratio,
        cap_ratio,
        frozenset(cmap),
    )


def _widths_of(path: str, face: int, codepoints: Iterable[int]) -> dict[int, int]:
    """Advance widths (1/1000 em) of ``codepoints`` in a font file."""
    try:
        font = TTFont(path, fontNumber=face, lazy=True)
        cmap = font.getBestCmap() or {}
        metrics = font["hmtx"].metrics
        upm = int(font["head"].unitsPerEm or 1000)
        out: dict[int, int] = {}
        for codepoint in codepoints:
            glyph = cmap.get(int(codepoint))
            if glyph is not None and glyph in metrics:
                out[int(codepoint)] = round(metrics[glyph][0] * 1000.0 / upm, 2)
        font.close()
    except (OSError, TTLibError, KeyError, ValueError, TypeError):
        return {}
    return out


# --------------------------------------------------------------------------- #
# resolver
# --------------------------------------------------------------------------- #
class FontResolver:
    """Section 8 pipeline with a per-document decision cache (step 7)."""

    def __init__(self, index: FontIndex | None = None) -> None:
        self.index = index if index is not None else default_index()
        self._families: dict[str, list[FontRecord]] | None = None
        self._decisions: dict[str, FontDecision] = {}
        self._coverage: dict[str, frozenset[int] | None] = {}
        self.stats = ResolverStats()

    # -- inventory ---------------------------------------------------------- #
    @property
    def families(self) -> dict[str, list[FontRecord]]:
        """Normalised family name -> faces (built once per resolver)."""
        if self._families is None:
            merged: dict[str, list[FontRecord]] = {}
            for record in self.index.records:
                merged.setdefault(_norm(record.family), []).append(record)
            self._families = merged
        return self._families

    def clear_cache(self) -> None:
        """Forget decisions (call after the document changed)."""
        self._decisions.clear()

    # -- public API --------------------------------------------------------- #
    def resolve(self, request: FontRequest) -> FontDecision:
        """Resolve ``request`` through section 8 steps 1-7 (cached, step 7)."""
        self.stats.requests += 1
        if request.cache_key:
            cached = self._decisions.get(request.cache_key)
            if cached is not None:
                self.stats.cache_hits += 1
                return cached
        decision = self._resolve(request)
        if request.cache_key:
            self._decisions[request.cache_key] = decision
        counter = {
            STRATEGY_REUSE: "reused",
            STRATEGY_SAME_FAMILY: "same_family",
            STRATEGY_NEAREST: "nearest",
            STRATEGY_SYNTHETIC: "synthetic",
            STRATEGY_SCRIPT: "script",
        }.get(decision.strategy)
        if counter is not None:
            setattr(self.stats, counter, getattr(self.stats, counter) + 1)
        return decision

    # -- pipeline ------------------------------------------------------------ #
    def _resolve(self, request: FontRequest) -> FontDecision:
        trace: list[str] = []
        program = request.program
        requested_name = _strip_subset(program.base_font or "unknown")
        needed = [cp for cp in request.codepoints if cp != 0x20]
        trace.append(
            f"original: {requested_name} "
            f"({'embedded' if program.embedded else 'not embedded'}, "
            f"weight {program.weight}, italic {program.italic}, "
            f"{len(request.text)} characters)"
        )
        if not request.text.strip():
            trace.append("step 0: empty text - nothing to resolve")
            return FontDecision(
                requested=requested_name,
                strategy=STRATEGY_REUSE,
                family=program.family,
                confidence=1.0,
                record=None,
                trace=tuple(trace),
                subset=False,
            )

        reuse = self._step_reuse(request, needed, trace)
        if reuse is not None:
            return reuse

        decision = self._step_pick(request, needed, trace)
        if decision is None:
            trace.append("step 3: index is empty - keep the original resource")
            return FontDecision(
                requested=requested_name,
                strategy=STRATEGY_REUSE,
                family=program.family,
                confidence=0.2,
                record=None,
                trace=tuple(trace),
                coverage_ok=False,
                subset=False,
            )

        decision = self._step_synthetic(request, decision, trace)
        decision = self._step_scripts(request, decision, trace)

        # step 6: the caller embeds decision.record (subset) + ToUnicode.
        trace.append(
            "step 6: embed subset of "
            + (decision.record.path if decision.record else "<original resource>")
        )
        decision.trace = tuple(trace)  # steps 4-6 ran after the snapshot
        return decision

    # step 1 ------------------------------------------------------------------ #
    def _step_reuse(
        self, request: FontRequest, needed: list[int], trace: list[str]
    ) -> FontDecision | None:
        program = request.program
        coverage: frozenset[int] | None = program.coverage
        if coverage is None and request.font_bytes is not None:
            coverage = codepoint_coverage(request.font_bytes, request.font_kind)
        if coverage is None:
            trace.append(
                "step 1: glyph table unknown "
                + ("(embedded subset)" if program.embedded else "(non-embedded)")
                + " - cannot reuse"
            )
            return None
        missing = [cp for cp in needed if cp not in coverage]
        if missing:
            sample = ", ".join(f"U+{cp:04X}" for cp in missing[:6])
            more = "" if len(missing) <= 6 else f" (+{len(missing) - 6} more)"
            trace.append(
                f"step 1: original font misses {len(missing)} glyph(s): "
                f"{sample}{more}"
            )
            return None
        trace.append(
            "step 1: original font covers every needed glyph - reuse resource"
        )
        return FontDecision(
            requested=_strip_subset(program.base_font or ""),
            strategy=STRATEGY_REUSE,
            family=program.family,
            confidence=0.99,
            record=None,
            trace=tuple(trace),
            subset=False,
        )

    # steps 2 + 3 -------------------------------------------------------------- #
    def _step_pick(
        self, request: FontRequest, needed: list[int], trace: list[str]
    ) -> FontDecision | None:
        program = request.program
        requested = _strip_subset(program.base_font or "")
        requested_key = _norm(program.family or requested)
        reference = None
        if request.font_bytes is not None:
            reference = _fp_from_program(
                request.font_bytes, request.font_kind, program.family,
                program.monospace,
            )
        orig_widths: dict[int, int] = {}
        if request.font_bytes is not None and needed:
            orig_widths = advance_widths(
                request.font_bytes, request.font_kind, needed
            )

        # -- step 2: same family, confirmed by advance widths ------------------
        for key in self._family_candidates(requested_key):
            records = self.families.get(key)
            if not records:
                continue
            record = self._best_face(
                records, request.target_weight, request.target_italic
            )
            verdict, confidence, note, metric_ok = self._confirm_metrics(
                record, orig_widths, exact=(key == requested_key), trace=trace
            )
            if verdict == "confirmed":
                trace.append(
                    f"step 2: same family '{record.family}' ({note}, "
                    f"confidence {confidence:.2f})"
                )
                decision = self._make_decision(
                    request, record, STRATEGY_SAME_FAMILY, confidence, note,
                    trace, metric_match=metric_ok,
                )
                decision.alternatives = tuple(
                    self._top_alternatives(
                        request, needed, reference, exclude=record, limit=3
                    )
                )
                return decision
            if verdict == "mismatch":
                trace.append(
                    f"step 2: '{record.family}' found but {note} - rejected"
                )

        # -- step 3: nearest by score -------------------------------------------
        scored = self._score_all(request, reference, requested_key)
        if not scored:
            return None
        trace.append("step 3: scored every bundled and system face")
        chosen: Candidate | None = None
        for candidate in scored:
            coverage = self._coverage_of(candidate.path, candidate.face)
            if coverage is None or all(cp in coverage for cp in needed):
                chosen = candidate
                break
        if chosen is None:
            chosen = scored[0]
            trace.append("step 3: no candidate covers the whole text")
        alternatives = _distinct_families(scored, chosen=chosen, limit=3)
        confidence = _confidence(chosen.score)
        trace.append(
            f"step 3: nearest is {chosen.display} ({chosen.reason}, "
            f"score {chosen.score:.2f}, confidence {confidence:.2f})"
        )
        decision = self._make_decision(
            request, chosen.record, STRATEGY_NEAREST, confidence, chosen.reason,
            trace, metric_match=("metrics match" in chosen.reason),
        )
        decision.alternatives = tuple(alternatives)
        return decision

    # step 4 ------------------------------------------------------------------- #
    def _step_synthetic(
        self, request: FontRequest, decision: FontDecision, trace: list[str]
    ) -> FontDecision:
        if decision.record is None:
            return decision
        faces = self.families.get(_norm(decision.record.family), [])
        has_italic = (
            any(face.italic for face in faces)
            if faces
            else decision.record.italic
        )
        has_bold = (
            any(face.weight >= 600 for face in faces)
            if faces
            else decision.record.weight >= 600
        )
        synthetic: list[str] = []
        if request.target_italic and not has_italic:
            synthetic.append("italic")
        if request.target_weight >= 600 and not has_bold:
            synthetic.append("bold")
        if not synthetic:
            return decision
        trace.append(
            f"step 4: '{decision.record.family}' has no "
            + "/".join(synthetic)
            + " face - synthesize (last resort)"
        )
        decision.strategy = STRATEGY_SYNTHETIC
        decision.synthetic = tuple(synthetic)
        decision.confidence = min(decision.confidence, 0.75)
        return decision

    # step 5 ------------------------------------------------------------------- #
    def _step_scripts(
        self, request: FontRequest, decision: FontDecision, trace: list[str]
    ) -> FontDecision:
        if decision.strategy == STRATEGY_REUSE:
            return decision
        runs = _split_scripts(request.text)
        if len(runs) <= 1:
            return decision
        resolved: list[ScriptRun] = []
        families_used: set[str] = {decision.family}
        for script, chunk in runs:
            family = decision.family
            path = decision.path
            face = decision.face
            label = _script_label(script)
            wanted = _SCRIPT_FAMILIES.get(script)
            if wanted is not None:
                records = self.families.get(_norm(wanted))
                if records:
                    record = self._best_face(
                        records, request.target_weight, False
                    )
                    family, path, face = record.family, record.path, record.face
                else:
                    trace.append(
                        f"step 5: bundled '{wanted}' missing - "
                        f"'{chunk[:12]}' keeps {decision.family}"
                    )
            elif script in _BASE_SCRIPTS:
                coverage = self._coverage_of(path, face) if path else None
                if coverage is None or any(ord(ch) not in coverage for ch in chunk):
                    nota = self.families.get(_norm("Noto Sans"))
                    fallback = (
                        self._best_face(nota, request.target_weight, False)
                        if nota
                        else None
                    )
                    if fallback is not None:
                        family, path, face = (
                            fallback.family, fallback.path, fallback.face
                        )
                        trace.append(
                            f"step 5: '{decision.family}' misses {label} glyphs "
                            "- run uses Noto Sans"
                        )
            elif script not in _BASE_SCRIPTS:
                trace.append(
                    f"step 5: no bundled family for {label} - "
                    f"'{chunk[:12]}' keeps {decision.family}"
                )
            if path is None and family != decision.family:
                family, path, face = decision.family, decision.path, decision.face
            families_used.add(family)
            resolved.append(ScriptRun(script, chunk, family, path, face))
        trace.append(
            f"step 5: {len(resolved)} script run(s), "
            f"{len(families_used)} famil(ies): "
            + ", ".join(sorted(families_used))
        )
        if len(families_used) == 1:
            return decision
        decision.strategy = STRATEGY_SCRIPT
        decision.runs = tuple(resolved)
        decision.confidence = min(decision.confidence, 0.9)
        return decision

    # helpers ------------------------------------------------------------------ #
    @staticmethod
    def _family_candidates(requested_key: str) -> list[str]:
        """Candidate family keys for step 2 (exact first, then equivalents)."""
        out: list[str] = []
        if requested_key:
            out.append(requested_key)
        for key, equivalents in _METRIC_EQUIVALENTS.items():
            if key == requested_key or requested_key.startswith(key):
                out.extend(equivalents)
        for key, equivalents in _METRIC_EQUIVALENTS.items():
            if requested_key in equivalents:
                out.append(key)
        seen: set[str] = set()
        ordered: list[str] = []
        for key in out:
            if key not in seen:
                seen.add(key)
                ordered.append(key)
        return ordered

    def _confirm_metrics(
        self,
        record: FontRecord,
        orig_widths: dict[int, int],
        *,
        exact: bool,
        trace: list[str],
    ) -> tuple[str, float, str, bool]:
        """Step 2: advance widths must match the subset within 1-2%."""
        if not orig_widths:
            if exact:
                return "confirmed", 0.9, "same family (no metric reference)", False
            return (
                "confirmed",
                0.85,
                "metric-compatible family by name (no metric reference)",
                False,
            )
        cand_widths = _widths_of(record.path, record.face, orig_widths.keys())
        if not cand_widths:
            return "mismatch", 0.0, "candidate font unreadable", False
        common = [cp for cp in orig_widths if cp in cand_widths]
        if not common:
            return "mismatch", 0.0, "no overlapping glyphs to compare", False
        diffs = [
            abs(cand_widths[cp] - orig_widths[cp]) / max(orig_widths[cp], 1.0)
            for cp in common
        ]
        mean = sum(diffs) / len(diffs)
        worst = max(diffs)
        if mean <= 0.015 and worst <= 0.03:
            note = f"advance widths within 1.5% over {len(common)} glyph(s)"
            trace.append(f"step 2: {record.family}: {note}")
            return "confirmed", _SAME_FAMILY_CONFIDENCE, note, True
        return (
            "mismatch",
            0.0,
            f"advance widths differ by {mean:.1%} on average",
            False,
        )

    @staticmethod
    def _best_face(
        records: list[FontRecord], weight: int, italic: bool
    ) -> FontRecord:
        def key(record: FontRecord) -> tuple[int, int]:
            return (abs(record.weight - weight), 0 if record.italic == italic else 1)

        return min(records, key=key)

    def _score_all(
        self,
        request: FontRequest,
        reference: _FP | None,
        requested_key: str,
    ) -> list[Candidate]:
        """Score every family, then score the faces of the best families."""
        wanted_class = _class_of(request.program.family, request.program.monospace)
        family_scores: list[tuple[float, str, str]] = []  # (score, key, reason)
        for key, records in self.families.items():
            representative = self._best_face(
                records, request.target_weight, request.target_italic
            )
            fingerprint = _fingerprint(representative)
            score, reason = _family_score(
                key, fingerprint, request, reference, wanted_class
            )
            family_scores.append((score, key, reason))
        family_scores.sort(
            key=lambda item: (
                -item[0],
                0 if self.families[item[1]][0].source == "bundled" else 1,
                item[1],
            )
        )

        candidates: list[Candidate] = []
        for family_score, key, reason in family_scores[:_MAX_TOP_FAMILIES]:
            for record in self.families[key]:
                face_score = _face_score(record, request)
                total = 0.72 * family_score + 0.28 * face_score
                if record.source == "bundled":
                    total = min(1.0, total + 0.01)
                candidates.append(
                    Candidate(record=record, score=round(total, 4), reason=reason)
                )
        candidates.sort(
            key=lambda c: (
                -c.score,
                0 if c.source == "bundled" else 1,
                c.family,
                c.subfamily,
            )
        )
        return candidates

    def _top_alternatives(
        self,
        request: FontRequest,
        needed: list[int],
        reference: _FP | None,
        *,
        exclude: FontRecord,
        limit: int = 3,
    ) -> list[Candidate]:
        requested_key = _norm(request.program.family or "")
        # Alternatives are badge-only: coverage is validated when the user
        # actually picks one, so we skip the (slow) cmap reads here.
        return _distinct_families(
            self._score_all(request, reference, requested_key),
            exclude=exclude,
            limit=limit,
        )

    def _make_decision(
        self,
        request: FontRequest,
        record: FontRecord,
        strategy: str,
        confidence: float,
        reason: str,
        trace: list[str],
        *,
        metric_match: bool = False,
    ) -> FontDecision:
        coverage = self._coverage_of(record.path, record.face)
        coverage_ok = coverage is None or all(
            cp in coverage for cp in request.codepoints
        )
        return FontDecision(
            requested=_strip_subset(request.program.base_font or ""),
            strategy=strategy,
            family=record.family,
            confidence=confidence,
            record=record,
            trace=tuple(trace),
            metric_match=metric_match,
            coverage_ok=coverage_ok,
            subset=True,
        )

    def _coverage_of(self, path: str, face: int) -> frozenset[int] | None:
        key = f"{path}#{face}"
        if key in self._coverage:
            return self._coverage[key]
        value = _coverage_of_file(path, face)
        self._coverage[key] = value
        return value


def _family_score(
    key: str,
    fingerprint: _FP | None,
    request: FontRequest,
    reference: _FP | None,
    wanted_class: str | None,
) -> tuple[float, str]:
    """Family-level part of the section-8 score (name/class/widths/heights)."""
    requested_key = _norm(request.program.family or "")
    bits: list[str] = []

    name_score = 0.0
    if requested_key:
        if requested_key == key:
            name_score = 1.0
            bits.append("same family")
        elif requested_key in _METRIC_EQUIVALENTS and (
            key in _METRIC_EQUIVALENTS[requested_key]
        ):
            name_score = 0.9
            bits.append("metric-compatible family")
        else:
            for name, equivalents in _METRIC_EQUIVALENTS.items():
                if key in equivalents and (
                    name == requested_key or requested_key in equivalents
                ):
                    name_score = 0.85
                    bits.append("metric-compatible family")
                    break
            if name_score == 0.0 and (
                requested_key in key or key in requested_key
            ):
                name_score = 0.7
                bits.append("related family name")

    class_score = 0.0
    candidate_class = fingerprint.family_class if fingerprint else None
    if wanted_class is not None and candidate_class is not None:
        class_score = 1.0 if wanted_class == candidate_class else 0.0
        bits.append("class match" if class_score else "class differs")

    parts = [(name_score, 0.34), (class_score, 0.16)]
    if reference is not None and fingerprint is not None:
        metric = _ratio_score(fingerprint.avg_advance, reference.avg_advance, 0.2)
        parts.append((metric, 0.24))
        if metric > 0.9:
            bits.append("metrics match")
        xcap = 0.0
        count = 0
        if reference.x_ratio and fingerprint.x_ratio:
            xcap += _ratio_score(fingerprint.x_ratio, reference.x_ratio, 0.4)
            count += 1
        if reference.cap_ratio and fingerprint.cap_ratio:
            xcap += _ratio_score(fingerprint.cap_ratio, reference.cap_ratio, 0.4)
            count += 1
        parts.append((xcap / count if count else 0.0, 0.12))

    total = sum(value * weight for value, weight in parts)
    weight_sum = sum(weight for _value, weight in parts)
    score = total / weight_sum if weight_sum else 0.5
    if not bits:
        bits.append("class/weight fit")
    return score, ", ".join(bits[:3])


def _face_score(record: FontRecord, request: FontRequest) -> float:
    """Face-level part of the section-8 score (weight + slant)."""
    weight_fit = 1.0 - min(abs(record.weight - request.target_weight), 500) / 500.0
    italic_fit = 1.0 if record.italic == request.target_italic else 0.0
    return 0.6 * weight_fit + 0.4 * italic_fit


def _confidence(score: float) -> float:
    """Map a 0..1 score onto the documented confidence scale."""
    return max(0.05, min(0.99, 0.30 + 0.72 * score))


def _split_scripts(text: str) -> list[tuple[str, str]]:
    """Split ``text`` into ``(script code, chunk)`` runs (step 5)."""
    scripts: list[str] = []
    last = ""
    for char in text:
        script = ft_unicodedata.script(char)
        if script in _NEUTRAL_SCRIPTS:
            script = last
        else:
            last = script
        scripts.append(script)
    first = next((script for script in scripts if script), "")
    runs: list[list[str]] = []
    for char, script in zip(text, scripts, strict=True):
        name = script or first
        if runs and runs[-1][0] == name:
            runs[-1][1] += char
        else:
            runs.append([name, char])
    return [(name, chunk) for name, chunk in runs if chunk]
