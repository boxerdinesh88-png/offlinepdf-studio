"""OfflinePDF Studio launcher (development entry point and frozen executable)."""

import sys


def _report_fatal_startup_error(exc: BaseException) -> None:
    """Frozen build only: save the traceback and explain the failure in a dialog.

    The windowed executable has no console, so without this an early failure
    is either invisible or a bare Python traceback.
    """
    import os
    import tempfile
    import traceback
    from pathlib import Path

    app_name = "OfflinePDF Studio"
    details = "".join(traceback.format_exception(exc))
    if sys.platform == "win32":
        data_root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / app_name
    elif sys.platform == "darwin":
        data_root = Path.home() / "Library" / "Application Support" / app_name
    else:
        data_root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / app_name

    report = None
    for folder in (data_root / "logs", Path(tempfile.gettempdir())):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            report = folder / "offlinepdf-startup-error.log"
            report.write_text(details, encoding="utf-8")
            break
        except OSError:
            report = None

    if isinstance(exc, PermissionError):
        reason = (
            f"{app_name} does not have permission to use a file or folder it needs:\n\n"
            f"{exc.filename or exc}\n\n"
            f"Settings and logs are kept in:\n{data_root}\n\n"
            "Make sure your user account can write to that folder (it must not be "
            "read-only or blocked by security software), then start the app again."
        )
    elif isinstance(exc, OSError):
        reason = (
            f"{app_name} could not access a file or folder it needs:\n\n{exc}\n\n"
            "Check that the disk is not full and the folder is not read-only, "
            "then start the app again."
        )
    elif isinstance(exc, ImportError):
        reason = (
            f"A component of {app_name} is missing or damaged:\n\n{exc}\n\n"
            "Please reinstall the application. If antivirus software removed "
            "files from the installation folder, restore them or add an exclusion."
        )
    else:
        reason = f"{app_name} could not start because of an unexpected error:\n\n{exc!r}"

    if report is not None:
        reason += f"\n\nTechnical details were saved to:\n{report}"

    shown = False
    if sys.platform == "win32":
        try:
            import ctypes

            mb_iconerror = 0x10
            ctypes.windll.user32.MessageBoxW(None, reason, f"{app_name} - startup error", mb_iconerror)
            shown = True
        except Exception:
            shown = False
    elif sys.platform == "darwin":
        try:
            import subprocess

            safe = reason.replace("\\", "\\\\").replace('"', '\\"')
            script = f'display alert "{app_name} - startup error" message "{safe}" as critical'
            subprocess.run(["osascript", "-e", script], check=False)
            shown = True
        except Exception:
            shown = False

    if not shown:
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            app = QApplication.instance() or QApplication([])
            QMessageBox.critical(None, f"{app_name} - startup error", reason)
            shown = True
        except Exception:
            pass
    if not shown:
        sys.stderr.write(reason + "\n")


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        try:
            from offlinepdf.app import main

            code = main()
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            _report_fatal_startup_error(exc)
            code = 1
        raise SystemExit(code)

    from offlinepdf.app import main

    raise SystemExit(main())
