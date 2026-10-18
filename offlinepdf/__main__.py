"""``python -m offlinepdf`` entry point."""

from __future__ import annotations

import sys

from offlinepdf.app import main

if __name__ == "__main__":
    sys.exit(main())
