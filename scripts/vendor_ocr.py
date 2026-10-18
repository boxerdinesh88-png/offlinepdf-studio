"""Vendor the offline OCR engine assets into ``assets/ocr`` (development time only).

The shipped application never downloads anything.  Models are fetched once by a
developer with network access, written into ``assets/ocr`` and bundled with the
app.  Every file is pinned by SHA-256 in ``assets/ocr/manifest.json``.

Usage::

    python scripts/vendor_ocr.py            # download what is missing
    python scripts/vendor_ocr.py --force    # re-download everything
    python scripts/vendor_ocr.py --verify   # hash check only, offline

Engine: rapidocr-onnxruntime (Apache-2.0) + ONNX Runtime (MIT).  The
recognition/detection models come from the PaddleOCR project (Apache-2.0).
Both are redistributed under terms recorded in THIRD_PARTY_LICENSES.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "assets" / "ocr"
MANIFEST = DEST / "manifest.json"
THIRD_PARTY = ROOT / "THIRD_PARTY_LICENSES.md"

USER_AGENT = "offlinepdf-studio-vendoring/1.0 (development-time script)"
BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2"

# (relative url, destination path under assets/ocr, human label)
FILES: tuple[tuple[str, str, str], ...] = (
    ("onnx/PP-OCRv4/det/en_PP-OCRv3_det_mobile.onnx", "en/det.onnx", "en det"),
    ("onnx/PP-OCRv4/rec/en_PP-OCRv4_rec_mobile.onnx", "en/rec.onnx", "en rec"),
    (
        "paddle/PP-OCRv4/rec/en_PP-OCRv4_rec_mobile/en_dict.txt",
        "en/keys.txt",
        "en keys",
    ),
    ("onnx/PP-OCRv4/det/ch_PP-OCRv4_det_mobile.onnx", "shared/det_cjk.onnx", "cjk det"),
    ("onnx/PP-OCRv4/rec/ch_PP-OCRv4_rec_mobile.onnx", "ch/rec.onnx", "ch rec"),
    (
        "paddle/PP-OCRv4/rec/ch_PP-OCRv4_rec_mobile/ppocr_keys_v1.txt",
        "ch/keys.txt",
        "ch keys",
    ),
    ("onnx/PP-OCRv4/det/multi_PP-OCRv3_det_mobile.onnx", "shared/det_multi.onnx", "multi det"),
    (
        "onnx/PP-OCRv4/rec/latin_PP-OCRv3_rec_mobile.onnx",
        "latin/rec.onnx",
        "latin rec",
    ),
    (
        "paddle/PP-OCRv4/rec/latin_PP-OCRv3_rec_mobile/latin_dict.txt",
        "latin/keys.txt",
        "latin keys",
    ),
    (
        "onnx/PP-OCRv4/rec/cyrillic_PP-OCRv3_rec_mobile.onnx",
        "cyrillic/rec.onnx",
        "cyrillic rec",
    ),
    (
        "paddle/PP-OCRv4/rec/cyrillic_PP-OCRv3_rec_mobile/cyrillic_dict.txt",
        "cyrillic/keys.txt",
        "cyrillic keys",
    ),
    (
        "onnx/PP-OCRv4/rec/devanagari_PP-OCRv4_rec_mobile.onnx",
        "devanagari/rec.onnx",
        "devanagari rec",
    ),
    (
        "paddle/PP-OCRv4/rec/devanagari_PP-OCRv4_rec_mobile/devanagari_dict.txt",
        "devanagari/keys.txt",
        "devanagari keys",
    ),
    ("onnx/PP-OCRv4/rec/arabic_PP-OCRv4_rec_mobile.onnx", "arabic/rec.onnx", "arabic rec"),
    (
        "paddle/PP-OCRv4/rec/arabic_PP-OCRv4_rec_mobile/arabic_dict.txt",
        "arabic/keys.txt",
        "arabic keys",
    ),
    ("onnx/PP-OCRv4/rec/japan_PP-OCRv4_rec_mobile.onnx", "japan/rec.onnx", "japan rec"),
    (
        "paddle/PP-OCRv4/rec/japan_PP-OCRv4_rec_mobile/japan_dict.txt",
        "japan/keys.txt",
        "japan keys",
    ),
    ("onnx/PP-OCRv4/rec/korean_PP-OCRv4_rec_mobile.onnx", "korean/rec.onnx", "korean rec"),
    (
        "paddle/PP-OCRv4/rec/korean_PP-OCRv4_rec_mobile/korean_dict.txt",
        "korean/keys.txt",
        "korean keys",
    ),
)

# Language code -> (display name, det file, rec file, keys file)
LANGUAGES: dict[str, tuple[str, str, str, str]] = {
    "en": ("English", "en/det.onnx", "en/rec.onnx", "en/keys.txt"),
    "ch": ("Chinese", "shared/det_cjk.onnx", "ch/rec.onnx", "ch/keys.txt"),
    "latin": ("Latin (multi)", "shared/det_multi.onnx", "latin/rec.onnx", "latin/keys.txt"),
    "cyrillic": ("Cyrillic", "shared/det_multi.onnx", "cyrillic/rec.onnx", "cyrillic/keys.txt"),
    "devanagari": (
        "Devanagari",
        "shared/det_multi.onnx",
        "devanagari/rec.onnx",
        "devanagari/keys.txt",
    ),
    "arabic": ("Arabic", "shared/det_multi.onnx", "arabic/rec.onnx", "arabic/keys.txt"),
    "japan": ("Japanese", "shared/det_cjk.onnx", "japan/rec.onnx", "japan/keys.txt"),
    "korean": ("Korean", "shared/det_cjk.onnx", "korean/rec.onnx", "korean/keys.txt"),
}


@dataclass(frozen=True)
class Entry:
    path: str
    label: str
    source: str
    sha256: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def load_manifest() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files", {})
    return files if isinstance(files, dict) else {}


def build_manifest(entries: list[Entry]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "comment": "generated by scripts/vendor_ocr.py - do not edit",
        "engine": "rapidocr-onnxruntime",
        "languages": {
            code: {"name": name, "det": det, "rec": rec, "keys": keys}
            for code, (name, det, rec, keys) in LANGUAGES.items()
        },
        "files": {
            e.path: {"label": e.label, "source": e.source, "sha256": e.sha256}
            for e in entries
        },
    }
    MANIFEST.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def verify() -> int:
    files = load_manifest()
    if not files:
        print("no manifest found - run scripts/vendor_ocr.py first")
        return 1
    failures = 0
    for rel, item in files.items():
        path = DEST / rel
        if not path.exists():
            print(f"MISSING {rel}")
            failures += 1
            continue
        if _sha256(path.read_bytes()) != item["sha256"]:
            print(f"CHANGED {rel}")
            failures += 1
    if failures:
        print(f"verify failed: {failures} problem(s)")
        return 1
    print(f"verify ok: {len(files)} files match the manifest")
    return 0


def vendor(force: bool) -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    known = load_manifest()
    entries: list[Entry] = []
    print("vendoring OCR models into", DEST)
    for rel_url, rel_path, label in FILES:
        dest = DEST / rel_path
        url = f"{BASE}/{rel_url}"
        if not force and dest.exists() and rel_path in known:
            entries.append(
                Entry(rel_path, label, url, _sha256(dest.read_bytes()))
            )
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"  {label}: {rel_path}")
        data = _fetch(url)
        dest.write_bytes(data)
        entries.append(Entry(rel_path, label, url, _sha256(data)))
    build_manifest(entries)
    _update_third_party()
    print(f"done: {len(entries)} models recorded in {MANIFEST.name}")
    return 0


def _update_third_party() -> None:
    marker = "<!-- OCR models -->"
    block = (
        f"{marker}\n\n"
        "- **PaddleOCR / PP-OCR models** (`assets/ocr/`) - Apache-2.0, "
        "Copyright (c) 2020 PaddlePaddle Authors. See "
        "<https://github.com/PaddlePaddle/PaddleOCR>.\n"
        "- **RapidOCR** (`rapidocr-onnxruntime`) - Apache-2.0, "
        "Copyright (c) RapidAI. See <https://github.com/RapidAI/RapidOCR>.\n"
        "- **ONNX Runtime** - MIT, Microsoft Corporation.\n"
        "- **OpenCV** (`opencv-python`) - Apache-2.0, Copyright (c) 2000-2024 "
        "Intel Corporation / Itseez.\n"
        "- **pyclipper** - MIT; **Shapely** - BSD-3-Clause; **PyYAML** - MIT; "
        "**six** - MIT; **protobuf** - BSD-3-Clause; **flatbuffers** - Apache-2.0.\n"
    )
    if not THIRD_PARTY.exists():
        THIRD_PARTY.write_text(f"# Third-party licenses\n\n{block}", encoding="utf-8")
        return
    text = THIRD_PARTY.read_text(encoding="utf-8")
    if marker in text:
        start = text.index(marker)
        end = text.find("\n# ", start)
        end = len(text) if end < 0 else end
        text = text[:start] + block + text[end:]
    else:
        text = text.rstrip() + "\n\n" + block
    THIRD_PARTY.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="re-download everything")
    parser.add_argument("--verify", action="store_true", help="check hashes only (no network)")
    args = parser.parse_args(argv)
    if args.verify:
        return verify()
    if DEST.exists() and args.force:
        shutil.rmtree(DEST)
    return vendor(args.force)


if __name__ == "__main__":
    sys.exit(main())
