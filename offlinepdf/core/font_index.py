"""Offline font inventory: bundled library + scanned system fonts.

Two sources feed every font decision:

* ``assets/fonts`` - the vendored library (Liberation, Carlito, Caladea, Noto),
  always present, never downloaded at runtime.
* the operating system's font folders - scanned once and cached to
  ``<data>/cache/font_index.json``; only files whose size/mtime changed are
  re-read, so later runs start instantly.

Nothing here touches the network.  The index is metadata only: glyph coverage
and metrics are read lazily from the file when a candidate font is actually
considered by :mod:`offlinepdf.core.font_resolver`.
"""

from __future__ import annotations

import json
import os
import string
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont, TTLibError

from offlinepdf.config import ensure_app_dirs, resource_path

__all__ = [
    "FontIndex",
    "FontRecord",
    "bundled_fonts_dir",
    "default_index",
    "system_font_dirs",
]

_CACHE_NAME = "font_index.json"
_CACHE_VERSION = 2
_FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}
_SCAN_LIMIT = 8192  # never let one pathological folder stall startup

#: Glyphs sampled to give every font an average advance width (1/1000 em).
REFERENCE_SAMPLE = string.ascii_lowercase + "etaoinsrhldlumcfigpybwvkxjqz0123456789 "


def bundled_fonts_dir() -> Path:
    """Directory holding the vendored font library."""
    return resource_path("assets", "fonts")


def system_font_dirs() -> list[Path]:
    """Well-known system font folders for the current platform."""
    dirs: list[Path] = []
    if sys.platform == "win32":
        windir = Path(os.environ.get("WINDIR") or r"C:\Windows")
        dirs += [windir / "Fonts"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs += [
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            Path.home() / "Library" / "Fonts",
        ]
    else:
        dirs += [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".local" / "share" / "fonts",
            Path.home() / ".fonts",
        ]
    return [path for path in dirs if path.is_dir()]


@dataclass(frozen=True)
class FontRecord:
    """One face (a file, or one member of a .ttc collection)."""

    path: str
    face: int
    family: str
    subfamily: str
    postscript_name: str
    weight: int
    italic: bool
    monospace: bool
    upm: int
    ascender: int
    descender: int
    cap_height: int
    x_height: int
    source: str
    size: int
    mtime: int
    # metric fingerprint used by font resolution (step 3 scoring)
    avg_advance: float = 0.0  # 1/1000 em over a reference glyph sample
    x_ratio: float = 0.0  # x-height / em
    cap_ratio: float = 0.0  # cap-height / em

    @property
    def key(self) -> str:
        return f"{self.path}#{self.face}"

    @property
    def regular(self) -> bool:
        """True for the upright, non-bold member of a family."""
        return not self.italic and self.weight < 600

    @property
    def display_name(self) -> str:
        return f"{self.family} {self.subfamily}".strip()

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "face": self.face,
            "family": self.family,
            "subfamily": self.subfamily,
            "postscript": self.postscript_name,
            "weight": self.weight,
            "italic": self.italic,
            "monospace": self.monospace,
            "upm": self.upm,
            "ascender": self.ascender,
            "descender": self.descender,
            "cap_height": self.cap_height,
            "x_height": self.x_height,
            "source": self.source,
            "size": self.size,
            "mtime": self.mtime,
            "avg_advance": round(self.avg_advance, 3),
            "x_ratio": round(self.x_ratio, 4),
            "cap_ratio": round(self.cap_ratio, 4),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FontRecord:
        return cls(
            path=str(raw.get("path", "")),
            face=int(raw.get("face", 0)),
            family=str(raw.get("family", "")),
            subfamily=str(raw.get("subfamily", "Regular")),
            postscript_name=str(raw.get("postscript", "")),
            weight=int(raw.get("weight", 400)),
            italic=bool(raw.get("italic", False)),
            monospace=bool(raw.get("monospace", False)),
            upm=int(raw.get("upm", 1000)),
            ascender=int(raw.get("ascender", 0)),
            descender=int(raw.get("descender", 0)),
            cap_height=int(raw.get("cap_height", 0)),
            x_height=int(raw.get("x_height", 0)),
            source=str(raw.get("source", "system")),
            size=int(raw.get("size", 0)),
            mtime=int(raw.get("mtime", 0)),
            avg_advance=float(raw.get("avg_advance", 0.0) or 0.0),
            x_ratio=float(raw.get("x_ratio", 0.0) or 0.0),
            cap_ratio=float(raw.get("cap_ratio", 0.0) or 0.0),
        )


@dataclass
class FontIndex:
    """Bundled + system font inventory with an incremental disk cache."""

    roots: list[tuple[Path, str]] = field(default_factory=list)
    cache_path: Path | None = None
    _records: list[FontRecord] = field(default_factory=list, repr=False)
    _loaded: bool = field(default=False, repr=False)

    # -- construction ----------------------------------------------------- #
    @classmethod
    def default(cls) -> FontIndex:
        """Bundled library first, then every system font folder."""
        roots = [(bundled_fonts_dir(), "bundled")]
        roots += [(path, "system") for path in system_font_dirs()]
        cache = ensure_app_dirs()["cache"] / _CACHE_NAME
        return cls(roots=roots, cache_path=cache)

    # -- scanning --------------------------------------------------------- #
    @property
    def records(self) -> list[FontRecord]:
        if not self._loaded:
            self.refresh()
        return self._records

    def refresh(self, *, force: bool = False) -> list[FontRecord]:
        """Re-scan changed files (or everything with ``force``) and re-cache."""
        cached = {} if force else self._load_cache()
        records: list[FontRecord] = []
        for root, source in self.roots:
            for path in self._font_files(root):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                records.extend(self._read_faces(path, source, stat, cached))
                if len(records) >= _SCAN_LIMIT:
                    break
            if len(records) >= _SCAN_LIMIT:
                break
        self._records = records
        self._loaded = True
        self._write_cache(records)
        return records

    def _font_files(self, root: Path) -> list[Path]:
        if root.is_file():
            return [root]
        if not root.is_dir():
            return []
        found: list[Path] = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if Path(name).suffix.lower() in _FONT_SUFFIXES:
                    found.append(Path(dirpath) / name)
            if len(found) >= _SCAN_LIMIT:
                break
        return sorted(found)

    def _read_faces(
        self,
        path: Path,
        source: str,
        stat: os.stat_result,
        cached: dict[str, dict[str, Any]],
    ) -> list[FontRecord]:
        key_prefix = f"{path}#"
        hits = [
            entry
            for key, entry in cached.items()
            if key.startswith(key_prefix)
            and int(entry.get("size", -1)) == stat.st_size
            and int(entry.get("mtime", -1)) == int(stat.st_mtime)
        ]
        if hits:
            return [FontRecord.from_dict(entry) for entry in hits]
        records: list[FontRecord] = []
        for face_index in _face_count(path):
            record = _read_record(path, face_index, source, stat)
            if record is not None:
                records.append(record)
        return records

    # -- cache ------------------------------------------------------------ #
    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if self.cache_path is None or not self.cache_path.exists():
            return {}
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or raw.get("version") != _CACHE_VERSION:
            return {}
        fonts = raw.get("fonts")
        if not isinstance(fonts, list):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for entry in fonts:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                out[f"{entry['path']}#{entry.get('face', 0)}"] = entry
        return out

    def _write_cache(self, records: Sequence[FontRecord]) -> None:
        if self.cache_path is None:
            return
        payload = {
            "version": _CACHE_VERSION,
            "fonts": [record.to_dict() for record in records],
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=0), encoding="utf-8")
        os.replace(tmp, self.cache_path)

    # -- queries ---------------------------------------------------------- #
    def families(self) -> dict[str, list[FontRecord]]:
        """Lower-cased family name -> every face of that family."""
        out: dict[str, list[FontRecord]] = {}
        for record in self.records:
            out.setdefault(record.family.lower(), []).append(record)
        return out

    def stats(self) -> dict[str, int]:
        records = self.records
        return {
            "total": len(records),
            "bundled": sum(1 for r in records if r.source == "bundled"),
            "system": sum(1 for r in records if r.source == "system"),
            "families": len({r.family.lower() for r in records}),
        }


def _face_count(path: Path) -> list[int]:
    """0 for a plain font file, 0..n-1 for a .ttc/.otc collection."""
    try:
        with open(path, "rb") as handle:
            magic = handle.read(12)
    except OSError:
        return []
    if magic[:4] == b"ttcf" and len(magic) == 12:
        count = int.from_bytes(magic[8:12], "big")
        if 0 < count <= 64:
            return list(range(count))
        return []
    return [0]


def _read_record(
    path: Path, face: int, source: str, stat: os.stat_result
) -> FontRecord | None:
    """Read the metadata one face needs; returns None when unreadable."""
    try:
        font = TTFont(str(path), fontNumber=face, lazy=True, ignoreDecompileErrors=True)
    except (OSError, TTLibError, TypeError, ValueError):
        return None
    try:
        name = font["name"]
        family = name.getDebugName(16) or name.getDebugName(1) or path.stem
        subfamily = name.getDebugName(17) or name.getDebugName(2) or "Regular"
        postscript = name.getDebugName(6) or ""
        os2 = font.get("OS/2")
        head = font.get("head")
        post = font.get("post")
        weight = 400
        italic = False
        cap_height = 0
        x_height = 0
        ascender = 0
        descender = 0
        if os2 is not None:
            weight = int(os2.usWeightClass or 400)
            italic = bool(int(os2.fsSelection) & 0x01)
            cap_height = int(getattr(os2, "sCapHeight", 0) or 0)
            x_height = int(getattr(os2, "sxHeight", 0) or 0)
            ascender = int(os2.sTypoAscender or 0)
            descender = int(os2.sTypoDescender or 0)
        upm = int(head.unitsPerEm) if head is not None else 1000
        if upm <= 0:
            upm = 1000
        if ascender == 0 and head is not None:
            ascender = int(getattr(head, "macStyle", 0) or 0) or int(
                getattr(font.get("hhea"), "ascent", 0) or 0
            )
        if descender == 0:
            descender = int(getattr(font.get("hhea"), "descent", 0) or 0)
        monospace = bool(post is not None and getattr(post, "isFixedPitch", 0))
        # metric fingerprint (step 3 scoring in font_resolver)
        avg_advance = 0.0
        try:
            cmap = font.getBestCmap() or {}
            hmtx = font["hmtx"].metrics
            widths = [
                hmtx[cmap[ord(char)]][0] * 1000.0 / upm
                for char in REFERENCE_SAMPLE
                if ord(char) in cmap and cmap[ord(char)] in hmtx
            ]
            if widths:
                avg_advance = sum(widths) / len(widths)
        except Exception:
            avg_advance = 0.0
        x_ratio = cap_ratio = 0.0
        if os2 is not None and upm:
            x_ratio = float(getattr(os2, "sxHeight", 0) or 0) / upm
            cap_ratio = float(getattr(os2, "sCapHeight", 0) or 0) / upm
        if not weight:
            weight = 400
        weight = max(1, min(900, weight))
        return FontRecord(
            path=str(path),
            face=face,
            family=family.strip(),
            subfamily=subfamily.strip(),
            postscript_name=postscript.strip(),
            weight=weight,
            italic=italic,
            monospace=monospace,
            upm=upm,
            ascender=ascender,
            descender=descender,
            cap_height=cap_height,
            x_height=x_height,
            source=source,
            size=int(stat.st_size),
            mtime=int(stat.st_mtime),
            avg_advance=avg_advance,
            x_ratio=x_ratio,
            cap_ratio=cap_ratio,
        )
    except Exception:
        return None
    finally:
        font.close()


_DEFAULT_INDEX: FontIndex | None = None


def default_index() -> FontIndex:
    """Process-wide index (scanned once, then served from memory)."""
    global _DEFAULT_INDEX
    if _DEFAULT_INDEX is None:
        _DEFAULT_INDEX = FontIndex.default()
        _DEFAULT_INDEX.refresh()
    return _DEFAULT_INDEX


def iter_records(index: FontIndex | None = None) -> Iterable[FontRecord]:
    """Every known face (bundled first - callers may rely on the order)."""
    records = (index or default_index()).records
    return sorted(records, key=lambda r: (r.source != "bundled", r.family, r.subfamily))
