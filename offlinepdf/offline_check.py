"""Static offline gate: fail the build when anything reaches for the network.

Scans project sources for remote URLs, CDN references, remote font/icon
imports and banned networking libraries.  The allowlist exists for license
texts, documentation, vendoring scripts (which legitimately download during
development) and the two modules whose *purpose* is to block networking.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Violation", "main", "scan_tree"]

# Files scanned relative to the project root.
_SCAN_GLOBS = (
    "main.py",
    "offlinepdf/**/*.py",
    "offlinepdf/**/*.json",
    "assets/**/*.json",
    "assets/**/*.html",
    "assets/**/*.htm",
    "assets/**/*.css",
    "assets/**/*.qss",
    "assets/**/*.svg",
    "assets/**/*.ui",
    "assets/**/*.xml",
    "packaging/**/*.py",
    "packaging/**/*.spec",
    "packaging/**/*.iss",
    "pyproject.toml",
)

# Never scanned: license texts, docs, vendoring scripts, tests (negative
# tests intentionally contain URLs), lock files.
_ALLOW_PREFIXES = (
    "THIRD_PARTY_LICENSES",
    "LICENSE",
    "OFL",
    "NOTICE",
    "docs/",
    "scripts/",
    "tests/",
    "vendor/",
    "assets/fonts/",
    "assets/ocr/",
    # These two modules exist to *prevent* network access; the selftest must
    # prove blocking with a real URL, netguard patches urllib by name.
    "offlinepdf/netguard.py",
    "offlinepdf/selftest.py",
    "offlinepdf/offline_check.py",
)

_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_CDN_RE = re.compile(
    r"\b(?:unpkg\.com|jsdelivr\.net|cdnjs\.(?:cloudflare|ajax)\.(?:com|net)|"
    r"fonts\.googleapis\.com|fonts\.gstatic\.com|cdn\.tailwindcss\.com|"
    r"ajax\.googleapis\.com|bootstrapcdn\.com)\b",
    re.IGNORECASE,
)
_IMPORT_RE = re.compile(
    r"^\s*(?:from|import)\s+("
    r"requests(?:\.\w+)*|httpx|aiohttp|urllib3|ftplib|smtplib|poplib|imaplib|"
    r"telnetlib|xmlrpc(?:\.\w+)*|paramiko|PySide6\.QtNetwork(?:\.\w+)*"
    r")\b",
    re.MULTILINE,
)
_QTNETWORK_USE_RE = re.compile(r"\bQNetworkAccessManager\b|\bQNetworkRequest\b")


@dataclass(frozen=True)
class Violation:
    """One forbidden pattern found in one file."""

    path: str
    line: int
    rule: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.excerpt.strip()!r}"


def _allowed(rel: str) -> bool:
    rel_posix = rel.replace("\\", "/")
    for prefix in _ALLOW_PREFIXES:
        if rel_posix.startswith(prefix) or rel_posix == prefix:
            return True
        name = Path(rel_posix).name
        if name.startswith(prefix):
            return True
    return False


def _iter_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for pattern in _SCAN_GLOBS:
        for path in root.glob(pattern):
            if path.is_file():
                files.add(path)
    return sorted(files)


def scan_tree(root: Path) -> list[Violation]:
    """Return every violation found under ``root``."""
    violations: list[Violation] = []
    for path in _iter_files(root):
        rel = str(path.relative_to(root))
        if _allowed(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _URL_RE.search(line):
                violations.append(Violation(rel, lineno, "remote-url", line))
            elif _CDN_RE.search(line):
                violations.append(Violation(rel, lineno, "cdn-reference", line))
            elif _IMPORT_RE.search(line):
                violations.append(Violation(rel, lineno, "network-import", line))
            elif _QTNETWORK_USE_RE.search(line):
                violations.append(Violation(rel, lineno, "qtnetwork", line))
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python scripts/check_offline.py [root]``. Exit 1 on violations."""
    parser = argparse.ArgumentParser(description="Fail when sources reference the network.")
    parser.add_argument(
        "root",
        nargs="?",
        default=str(Path(__file__).resolve().parent.parent),
        help="project root (default: repository root)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)
    violations = scan_tree(root)
    if violations:
        print(f"OFFLINE CHECK FAILED: {len(violations)} violation(s)")
        for violation in violations:
            print(f"  {violation}")
        return 1
    print("offline check passed: no remote URLs, CDNs or network libraries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
