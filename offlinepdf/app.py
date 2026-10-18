"""Application bootstrap: logging first, network guard second, then Qt."""

from __future__ import annotations

import atexit
import logging
from pathlib import Path

from offlinepdf import netguard

__all__ = ["main", "run_ui"]

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Console entry point (also used by ``main.py``)."""
    import argparse

    from offlinepdf.config import version_banner
    from offlinepdf.logging_setup import setup_logging

    parser = argparse.ArgumentParser(prog="offlinepdf", description=__doc__)
    parser.add_argument("file", nargs="?", help="PDF file to open")
    parser.add_argument(
        "--selftest-offline",
        action="store_true",
        help="run the offline smoke test (no GUI) and exit",
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args(argv)

    if args.version:
        print(version_banner())
        return 0

    setup_logging()
    installed = netguard.install()
    logger.info("bootstrap: netguard installed=%s, %s", installed, version_banner())
    atexit.register(_cleanup_on_exit)

    if args.selftest_offline:
        from offlinepdf.selftest import run_selftest

        return run_selftest()

    return run_ui(Path(args.file) if args.file else None)


def run_ui(start_file: Path | None = None) -> int:
    """Create the QApplication and enter the event loop."""
    import sys
    from typing import cast

    from PySide6.QtWidgets import QApplication

    from offlinepdf import APP_NAME, __version__
    from offlinepdf.config import Settings
    from offlinepdf.ui.main_window import MainWindow
    from offlinepdf.ui.theme import apply_theme

    app = cast(QApplication, QApplication.instance() or QApplication(sys.argv[:1]))
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)

    settings = Settings()
    apply_theme(app, settings.get_str("theme", "light") == "dark")  # type: ignore[arg-type]

    window = MainWindow(settings)
    window.show()
    if start_file is not None:
        window.open_path(start_file)
    return app.exec()


def _cleanup_on_exit() -> None:
    """Wipe app-owned temp files at process exit."""
    from offlinepdf.core.recovery import TempWorkspace

    try:
        removed = TempWorkspace().wipe()
        if removed:
            logger.info("wiped %d temp files on exit", removed)
    except Exception:
        logger.exception("temp cleanup failed")
