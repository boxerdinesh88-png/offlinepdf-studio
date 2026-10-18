"""App-owned temp workspace and crash-recovery store.

Temp files live only under the app folder and are wiped on clean exit.
Recovery files survive crashes and are removed when the user saves.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from offlinepdf.config import ensure_app_dirs

__all__ = ["RecoveryStore", "TempWorkspace", "purge_stale_recovery"]

logger = logging.getLogger(__name__)

_RECOVERY_MAX_AGE_DAYS = 30


class TempWorkspace:
    """Unique temp files inside the app folder; wiped on exit."""

    def __init__(self, base: Path | None = None) -> None:
        self.base = base or ensure_app_dirs()["temp"]

    def new_file(self, prefix: str = "tmp", suffix: str = "") -> Path:
        """Return a fresh, non-existent temp path."""
        self.base.mkdir(parents=True, exist_ok=True)
        return self.base / f"{prefix}-{uuid.uuid4().hex}{suffix}"

    def wipe(self) -> int:
        """Delete everything inside the temp folder. Returns item count."""
        removed = 0
        if not self.base.exists():
            return 0
        for entry in self.base.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
            except OSError:
                logger.warning("could not remove temp entry %s", entry)
        return removed


class RecoveryStore:
    """Autosave copies of open documents, keyed by source path."""

    def __init__(self, base: Path | None = None) -> None:
        self.base = base or ensure_app_dirs()["recovery"]

    @staticmethod
    def _key(source: Path) -> str:
        return hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:24]

    def file_for(self, source: Path) -> Path:
        return self.base / f"{self._key(source)}.pdf"

    def meta_for(self, source: Path) -> Path:
        return self.base / f"{self._key(source)}.json"

    def write(self, source: Path, data: bytes) -> Path:
        """Atomically store an autosave copy of ``data`` for ``source``."""
        self.base.mkdir(parents=True, exist_ok=True)
        target = self.file_for(source)
        tmp = target.with_suffix(".pdf.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        meta = {
            "source": str(source),
            "saved_at": time.time(),
            "size": len(data),
        }
        self.meta_for(source).write_text(json.dumps(meta), encoding="utf-8")
        logger.info("recovery snapshot written for %s (%d bytes)", source.name, len(data))
        return target

    def read(self, source: Path) -> bytes | None:
        """Return stored recovery bytes for ``source`` or None."""
        target = self.file_for(source)
        try:
            return target.read_bytes()
        except OSError:
            return None

    def meta(self, source: Path) -> dict[str, object] | None:
        """Return metadata for the stored snapshot or None."""
        try:
            raw = json.loads(self.meta_for(source).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def delete(self, source: Path) -> None:
        """Remove the snapshot for ``source`` (called after a real save)."""
        self.file_for(source).unlink(missing_ok=True)
        self.meta_for(source).unlink(missing_ok=True)

    def clear(self) -> None:
        """Remove every snapshot."""
        if not self.base.exists():
            return
        for entry in self.base.iterdir():
            entry.unlink(missing_ok=True)


def purge_stale_recovery(base: Path | None = None) -> int:
    """Delete recovery files older than the retention window. Returns count."""
    store_dir = base or ensure_app_dirs()["recovery"]
    if not store_dir.exists():
        return 0
    cutoff = time.time() - _RECOVERY_MAX_AGE_DAYS * 86400
    removed = 0
    for entry in store_dir.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("purged %d stale recovery files", removed)
    return removed
