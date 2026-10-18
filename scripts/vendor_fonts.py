"""Vendor the offline font library into ``assets/fonts`` (development time only).

The application never downloads anything: fonts are fetched once by a developer
with network access, written into ``assets/fonts`` and shipped with the app.
Every file is pinned by SHA-256 in ``assets/fonts/manifest.json`` so the script
is reproducible and ``--verify`` can re-check the tree without network.

Usage::

    python scripts/vendor_fonts.py            # download what is missing
    python scripts/vendor_fonts.py --force    # re-download everything
    python scripts/vendor_fonts.py --verify   # hash check only, offline

Sources (all open licences, see THIRD_PARTY_LICENSES.md):

* Liberation Sans/Serif/Mono - metric compatible with Arial/Times/Courier,
  taken from the Debian/Ubuntu ``fonts-liberation2`` binary package.
* Carlito - metric compatible with Calibri (Google Fonts, OFL-1.1).
* Caladea - metric compatible with Cambria (Google Fonts, OFL-1.1).
* Noto Sans (Latin/Greek/Cyrillic), Noto Sans Arabic / Hebrew / Devanagari /
  SC - Google Fonts OFL sources, instantiated to static Regular + Bold at
  vendor time with fontTools.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import lzma
import shutil
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "assets" / "fonts"
MANIFEST = DEST / "manifest.json"
THIRD_PARTY = ROOT / "THIRD_PARTY_LICENSES.md"

USER_AGENT = "offlinepdf-studio-vendoring/1.0 (development-time script)"
GOOGLE_RAW = "https://raw.githubusercontent.com/google/fonts/main/"
UBUNTU_DEB = (
    "https://archive.ubuntu.com/ubuntu/pool/main/f/fonts-liberation/"
    "fonts-liberation_2.1.5-3_all.deb"
)

# Static files straight out of the Google Fonts repository.
GOOGLE_STATIC: tuple[tuple[str, str, str], ...] = (
    # (relative path in google/fonts, destination family dir, licence file)
    ("ofl/carlito/Carlito-Regular.ttf", "Carlito", "ofl/carlito/OFL.txt"),
    ("ofl/carlito/Carlito-Bold.ttf", "Carlito", "ofl/carlito/OFL.txt"),
    ("ofl/carlito/Carlito-Italic.ttf", "Carlito", "ofl/carlito/OFL.txt"),
    ("ofl/carlito/Carlito-BoldItalic.ttf", "Carlito", "ofl/carlito/OFL.txt"),
    ("ofl/caladea/Caladea-Regular.ttf", "Caladea", "ofl/caladea/OFL.txt"),
    ("ofl/caladea/Caladea-Bold.ttf", "Caladea", "ofl/caladea/OFL.txt"),
    ("ofl/caladea/Caladea-Italic.ttf", "Caladea", "ofl/caladea/OFL.txt"),
    ("ofl/caladea/Caladea-BoldItalic.ttf", "Caladea", "ofl/caladea/OFL.txt"),
)

# Variable fonts instantiated to static weights at vendor time.
GOOGLE_VARIABLE: tuple[tuple[str, str, str], ...] = (
    # (relative path, destination family dir, licence file)
    ("ofl/notosans/NotoSans%5Bwdth%2Cwght%5D.ttf", "NotoSans", "ofl/notosans/OFL.txt"),
    (
        "ofl/notosansarabic/NotoSansArabic%5Bwdth%2Cwght%5D.ttf",
        "NotoSansArabic",
        "ofl/notosansarabic/OFL.txt",
    ),
    (
        "ofl/notosanshebrew/NotoSansHebrew%5Bwdth%2Cwght%5D.ttf",
        "NotoSansHebrew",
        "ofl/notosanshebrew/OFL.txt",
    ),
    (
        "ofl/notosansdevanagari/NotoSansDevanagari%5Bwdth%2Cwght%5D.ttf",
        "NotoSansDevanagari",
        "ofl/notosansdevanagari/OFL.txt",
    ),
    ("ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf", "NotoSansSC", "ofl/notosanssc/OFL.txt"),
)

# (style name, axis pinning) for every static instance.
INSTANCES: tuple[tuple[str, dict[str, int]], ...] = (
    ("Regular", {"wght": 400, "wdth": 100}),
    ("Bold", {"wght": 700, "wdth": 100}),
)

LICENSE_ID = "OFL-1.1"


@dataclass
class Entry:
    """One vendored file recorded in the manifest."""

    path: str
    family: str
    style: str
    license: str
    source: str
    sha256: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "family": self.family,
            "style": self.style,
            "license": self.license,
            "source": self.source,
            "sha256": self.sha256,
        }


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(rel_path: str, data: bytes, entries: list[Entry], *, family: str,
           style: str, source: str) -> None:
    target = DEST / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    entries.append(
        Entry(rel_path, family, style, LICENSE_ID, source, _sha256(data))
    )


def _ar_members(blob: bytes) -> dict[str, bytes]:
    """Minimal GNU ``ar`` reader (only what a .deb needs)."""
    if not blob.startswith(b"!<arch>\n"):
        raise ValueError("not an ar archive")
    pos = 8
    members: dict[str, bytes] = {}
    while pos + 60 <= len(blob):
        header = blob[pos : pos + 60]
        name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        pos += 60
        members[name] = blob[pos : pos + size]
        pos += size + (size % 2)
    return members


def _zstd_decompress(blob: bytes) -> bytes:
    """zstd support: stdlib on 3.14+, the ``zstandard`` backport otherwise."""
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        try:
            import zstandard
        except ImportError as exc:  # pragma: no cover - dev environment
            raise SystemExit(
                "data.tar.zst needs Python 3.14 (compression.zstd) or the "
                "'zstandard' package"
            ) from exc
        return zstandard.ZstdDecompressor().decompress(blob)
    return zstd.decompress(blob)


def _deb_data(deb: bytes) -> bytes:
    for name, blob in _ar_members(deb).items():
        if not name.startswith("data.tar"):
            continue
        if name.endswith(".xz"):
            return lzma.decompress(blob)
        if name.endswith(".gz"):
            return gzip.decompress(blob)
        if name.endswith(".zst"):
            return _zstd_decompress(blob)
        raise SystemExit(f"unsupported data.tar variant: {name}")
    raise SystemExit("no data.tar.* member found in the .deb")


def vendor_liberation(entries: list[Entry], force: bool) -> None:
    """Liberation Sans/Serif/Mono from the Ubuntu fonts-liberation2 package."""
    print(f"  liberation: {UBUNTU_DEB}")
    data = _deb_data(_download(UBUNTU_DEB))
    picked = 0
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            name = Path(member.name).name
            if not member.isfile() or not name.endswith(".ttf"):
                continue
            family = name.split("-", 1)[0]
            style = name.split("-", 1)[1][: -len(".ttf")]
            if family not in ("LiberationSans", "LiberationSerif", "LiberationMono"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            blob = handle.read()
            rel = f"{family}/{family}-{style}.ttf"
            if not force and (DEST / rel).exists():
                continue
            _write(rel, blob, entries, family=family, style=style, source=UBUNTU_DEB)
            picked += 1
            print(f"    {rel} ({len(blob) // 1024} KiB)")
        # Debian copyright file documents the OFL grant for these files.
        for member in tar.getmembers():
            if member.isfile() and member.name.endswith("/copyright"):
                handle = tar.extractfile(member)
                if handle is not None and (
                    force or not (DEST / "licenses" / "liberation-copyright.txt").exists()
                ):
                    (DEST / "licenses").mkdir(parents=True, exist_ok=True)
                    (DEST / "licenses" / "liberation-copyright.txt").write_bytes(
                        handle.read()
                    )
                break
    if picked == 0 and not (DEST / "LiberationSans").exists():
        raise SystemExit("no Liberation fonts found in the package")


def vendor_google_static(entries: list[Entry], force: bool) -> None:
    for rel, family, lic in GOOGLE_STATIC:
        name = Path(rel.replace("%5B", "[").replace("%5D", "]")).name
        style = name[: -len(".ttf")].split("-")[-1]
        dest = f"{family}/{name}"
        if not force and (DEST / dest).exists():
            continue
        url = GOOGLE_RAW + rel
        print(f"  {dest}")
        _write(dest, _download(url), entries, family=family, style=style, source=url)
        _write_license(family, GOOGLE_RAW + lic, force)


def vendor_google_variable(entries: list[Entry], force: bool) -> None:
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    for rel, family, lic in GOOGLE_VARIABLE:
        url = GOOGLE_RAW + rel
        print(f"  {family}: instantiating static weights")
        payload = _bypass_cache(url)
        for style, axes in INSTANCES:
            dest = f"{family}/{family}-{style}.ttf"
            if not force and (DEST / dest).exists():
                continue
            font = TTFont(io.BytesIO(payload))
            available = {axis.axisTag for axis in font["fvar"].axes}
            pins = {k: v for k, v in axes.items() if k in available}
            instantiateVariableFont(font, pins, inplace=True, updateFontNames=True)
            buffer = io.BytesIO()
            font.save(buffer)
            blob = buffer.getvalue()
            font.close()
            _write(dest, blob, entries, family=family, style=style, source=url)
            print(f"    {dest} ({len(blob) // 1024} KiB)")
        _write_license(family, GOOGLE_RAW + lic, force)


_CACHE: dict[str, bytes] = {}


def _bypass_cache(url: str) -> bytes:
    if url not in _CACHE:
        _CACHE[url] = _download(url)
    return _CACHE[url]


def _write_license(family: str, url: str, force: bool) -> None:
    target = DEST / family / "LICENSE.txt"
    if target.exists() and not force:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_download(url))


def build_manifest(entries: list[Entry]) -> None:
    entries.sort(key=lambda e: e.path)
    payload = {
        "comment": "generated by scripts/vendor_fonts.py - do not edit",
        "fonts": [entry.to_dict() for entry in entries],
    }
    MANIFEST.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    write_third_party(entries)


def write_third_party(entries: list[Entry]) -> None:
    by_family: dict[str, list[Entry]] = {}
    for entry in entries:
        by_family.setdefault(entry.family, []).append(entry)
    lines = [
        "# Third-party components",
        "",
        "OfflinePDF Studio itself is AGPL-3.0-or-later. The components below are",
        "bundled as files and are licensed under their own terms. Every font is",
        "vendored into `assets/fonts/` by `scripts/vendor_fonts.py` (development",
        "time only); the application never downloads fonts at runtime.",
        "",
        "## Font library (`assets/fonts/`)",
        "",
        "| Family | Files | Licence | Purpose |",
        "| --- | ---: | --- | --- |",
    ]
    purposes = {
        "LiberationSans": "metric-compatible replacement for Arial",
        "LiberationSerif": "metric-compatible replacement for Times New Roman",
        "LiberationMono": "metric-compatible replacement for Courier New",
        "Carlito": "metric-compatible replacement for Calibri",
        "Caladea": "metric-compatible replacement for Cambria",
        "NotoSans": "Latin / Greek / Cyrillic script coverage",
        "NotoSansArabic": "Arabic script coverage",
        "NotoSansHebrew": "Hebrew script coverage",
        "NotoSansDevanagari": "Devanagari script coverage",
        "NotoSansSC": "Simplified Chinese (CJK) coverage",
    }
    for family in sorted(by_family):
        group = by_family[family]
        lines.append(
            f"| {family} | {len(group)} | {LICENSE_ID} | {purposes.get(family, '-')} |"
        )
    lines += [
        "",
        "Licence texts: `assets/fonts/<Family>/LICENSE.txt` (OFL-1.1) and",
        "`assets/fonts/licenses/liberation-copyright.txt` (Debian copyright file",
        "for the Liberation packages, which are also OFL-1.1).",
        "",
        "SHA-256 checksums for every bundled file: `assets/fonts/manifest.json`.",
        "",
        "## Python dependencies",
        "",
        "See `requirements.lock` for the pinned stack (PySide6, PyMuPDF, pikepdf,",
        "fontTools, uharfbuzz, numpy, Pillow, reportlab) and the `LICENSE` files",
        "inside the installed distributions.",
        "",
    ]
    THIRD_PARTY.write_text("\n".join(lines), encoding="utf-8")


def load_manifest() -> dict[str, dict[str, str]]:
    if not MANIFEST.exists():
        return {}
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {item["path"]: item for item in payload.get("fonts", [])}


def verify() -> int:
    """Re-hash the tree without touching the network."""
    entries = load_manifest()
    if not entries:
        print("no manifest found - run scripts/vendor_fonts.py first")
        return 1
    failures = 0
    for rel, item in entries.items():
        path = DEST / rel
        if not path.exists():
            print(f"MISSING {rel}")
            failures += 1
            continue
        digest = _sha256(path.read_bytes())
        if digest != item["sha256"]:
            print(f"CHANGED {rel}")
            failures += 1
    if failures:
        print(f"verify failed: {failures} problem(s)")
        return 1
    print(f"verify ok: {len(entries)} files match the manifest")
    return 0


def vendor(force: bool) -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    known = load_manifest()
    entries: list[Entry] = []
    print("vendoring fonts into", DEST)
    vendor_liberation(entries, force)
    vendor_google_static(entries, force)
    vendor_google_variable(entries, force)
    # Files that already existed (not re-downloaded) still belong in the
    # manifest: re-hash whatever is on disk for the families we touched.
    for rel, item in known.items():
        path = DEST / rel
        if path.exists() and not any(e.path == rel for e in entries):
            entries.append(
                Entry(rel, item["family"], item["style"], item["license"],
                      item["source"], _sha256(path.read_bytes()))
            )
    build_manifest(entries)
    print(f"done: {len(entries)} fonts recorded in {MANIFEST.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="re-download everything")
    parser.add_argument(
        "--verify", action="store_true", help="check hashes only (no network)"
    )
    args = parser.parse_args(argv)
    if args.verify:
        return verify()
    if DEST.exists() and args.force:
        shutil.rmtree(DEST)
    return vendor(args.force)


if __name__ == "__main__":
    sys.exit(main())
