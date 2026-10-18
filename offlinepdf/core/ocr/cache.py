"""On-disk cache of recognised lines, keyed by what the page looks like.

The key hashes a small rendering of the page plus every option that changes
the result, so reopening a document (or the same scan in another file) never
repeats OCR, while any visual change to the page misses the cache.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from pathlib import Path

from .layout import OcrLine

__all__ = ["OcrCache"]

logger = logging.getLogger(__name__)

_VERSION = 14  # 14: dropped spaces restored
_MAX_ENTRIES = 3000


class OcrCache:
    """JSON files under ``<data>/cache/ocr`` (best effort; never raises)."""

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            from offlinepdf.config import ensure_app_dirs

            root = ensure_app_dirs()["cache"] / "ocr"
        self.root = root

    @staticmethod
    def key(fingerprint: bytes, options: str) -> str:
        digest = hashlib.sha256()
        digest.update(f"v{_VERSION}|{options}|".encode())
        digest.update(fingerprint)
        return digest.hexdigest()[:40]

    def get(self, key: str) -> list[OcrLine] | None:
        path = self.root / f"{key}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [OcrLine.from_dict(item) for item in data["lines"]]
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning("ignoring unreadable OCR cache entry %s", path.name)
            return None

    def put(self, key: str, lines: list[OcrLine]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {"lines": [line.to_dict() for line in lines]}
            tmp = self.root / f"{key}.tmp"
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.root / f"{key}.json")
            self._prune()
        except OSError as exc:
            logger.warning("OCR cache write failed: %s", exc)

    def clear(self) -> int:
        removed = 0
        for path in self.root.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def _prune(self) -> None:
        entries = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for path in entries[: max(0, len(entries) - _MAX_ENTRIES)]:
            with contextlib.suppress(OSError):
                path.unlink()
