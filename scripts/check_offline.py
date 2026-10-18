"""CLI wrapper around offlinepdf.offline_check (kept for scripts/ layout)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from offlinepdf.offline_check import main

if __name__ == "__main__":
    sys.exit(main())
