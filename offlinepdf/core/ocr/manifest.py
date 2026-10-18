"""OCR language packs: which ONNX models are bundled and where they live.

The vendored model library under ``assets/ocr`` is described by a single
manifest (``assets/ocr/manifest.json``) written by ``scripts/vendor_ocr.py``.
Each language entry maps a code to its detection / recognition ONNX files and
an optional keys file.  Everything here is read from disk - there is never a
download at runtime (see ``docs/DECISIONS.md``).

The rec ONNX models embed the character list in their metadata, so the keys
file is not required for inference; it is kept in the manifest for reference
and for any pack that does not embed it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from offlinepdf.config import resource_path

__all__ = [
    "LanguagePack",
    "available_languages",
    "default_language",
    "ocr_assets_dir",
    "require_language",
    "verify_assets",
]

#: Engine version the bundled models belong to (informational).
_ENGINE = "rapidocr-onnxruntime"

#: Language used when the user does not ask for a specific one.
def default_language() -> str:
    return "en"


def ocr_assets_dir() -> Path:
    """Directory holding the vendored OCR assets."""
    return resource_path("assets", "ocr")


@dataclass(frozen=True)
class LanguagePack:
    """Resolved files for one OCR language."""

    code: str
    name: str
    det_model: Path
    rec_model: Path
    keys: Path | None = None

    @property
    def installed(self) -> bool:
        return self.det_model.is_file() and self.rec_model.is_file()


def _load_manifest(root: Path | None = None) -> dict:
    path = (root or ocr_assets_dir()) / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"OCR manifest missing: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _pack_from(manifest: dict, code: str, base: Path) -> LanguagePack:
    entry = manifest.get("languages", {}).get(code)
    if not isinstance(entry, dict):
        raise KeyError(f"no OCR language pack named {code!r}")
    rel_det = entry.get("det")
    rel_rec = entry.get("rec")
    rel_keys = entry.get("keys")
    if not rel_det or not rel_rec:
        raise ValueError(f"language {code!r} missing det/rec paths")
    return LanguagePack(
        code=code,
        name=entry.get("name", code),
        det_model=base / rel_det,
        rec_model=base / rel_rec,
        keys=base / rel_keys if rel_keys else None,
    )


def available_languages(root: Path | None = None) -> list[LanguagePack]:
    """Every language whose model files exist on disk, sorted by name."""
    manifest = _load_manifest(root)
    base = root or ocr_assets_dir()
    packs = [_pack_from(manifest, code, base) for code in manifest.get("languages", {})]
    return sorted((pack for pack in packs if pack.installed), key=lambda pack: pack.name.lower())


def require_language(code: str, root: Path | None = None) -> LanguagePack:
    """Resolve ``code`` or raise :class:`KeyError` (missing code or files)."""
    base = root or ocr_assets_dir()
    manifest = _load_manifest(base)
    entry = manifest.get("languages", {}).get(code)
    if entry is None:
        raise KeyError(f"no OCR language pack named {code!r}")
    pack = _pack_from(manifest, code, base)
    if not pack.installed:
        raise KeyError(f"OCR language pack {code!r} is not fully installed")
    return pack


def verify_assets(root: Path | None = None) -> tuple[bool, list[str]]:
    """Rehash every manifest file against disk; returns (ok, problems).

    This is the offline integrity check - it never downloads anything.
    """
    base = root or ocr_assets_dir()
    manifest = _load_manifest(base)
    problems: list[str] = []
    for rel, meta in manifest.get("files", {}).items():
        expected = meta.get("sha256")
        path = base / rel
        if not path.is_file():
            problems.append(f"{rel}: missing")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected and digest != expected:
            problems.append(f"{rel}: hash mismatch (got {digest[:12]}…)")
    return not problems, problems
