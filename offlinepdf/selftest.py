"""``--selftest-offline``: prove the app works with networking hard-blocked.

Runs headless (no Qt event loop): network guard probes, document open/save/
verify round-trip, recovery, rendering, static offline scan and temp wipe.
Exit code 0 only when every check passes.
"""

from __future__ import annotations

import logging
import socket
import sys
import urllib.request
from collections.abc import Callable

from offlinepdf import netguard
from offlinepdf.config import version_banner

__all__ = ["run_selftest"]

logger = logging.getLogger(__name__)


class _Result:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.lines: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        mark = "PASS" if ok else "FAIL"
        self.lines.append(f"  [{mark}] {name}" + (f" - {detail}" if detail else ""))
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        return ok


def run_selftest() -> int:
    """Run every offline smoke check. Returns a process exit code."""
    print(version_banner())
    print("offline selftest (all network APIs disabled)\n")
    result = _Result()

    # 1. Network guard must already be installed and provably blocking.
    result.check("netguard installed", netguard.is_installed())
    netguard.clear_attempts()

    def expect_blocked(name: str, fn: Callable[[], object]) -> bool:
        try:
            fn()
        except netguard.NetworkBlockedError:
            return True
        except Exception as exc:
            result.lines.append(f"         {name} raised {type(exc).__name__}: {exc}")
            return False
        result.lines.append(f"         {name} did NOT raise")
        return False

    result.check(
        "socket.connect blocked",
        expect_blocked("connect", lambda: socket.create_connection(("127.0.0.1", 80), 1)),
    )
    result.check(
        "DNS getaddrinfo blocked",
        expect_blocked("getaddrinfo", lambda: socket.getaddrinfo("example.com", 443)),
    )
    result.check(
        "urllib.urlopen blocked",
        expect_blocked(
            "urlopen",
            lambda: urllib.request.urlopen("http://127.0.0.1/probe", timeout=1),
        ),
    )
    result.check(
        "attempts logged",
        len(netguard.attempts()) >= 3,
        f"{len(netguard.attempts())} logged",
    )

    # 2. Static source scan.
    try:
        from pathlib import Path

        from offlinepdf.offline_check import scan_tree

        root = Path(__file__).resolve().parent.parent
        violations = scan_tree(root) if (root / "pyproject.toml").exists() else []
        result.check("static offline scan", not violations, f"{len(violations)} violation(s)")
        for violation in violations[:5]:
            result.lines.append(f"         {violation}")
    except Exception as exc:
        result.check("static offline scan", False, str(exc))

    # 3. Document lifecycle in the app-owned temp folder.
    from offlinepdf.core.document import (
        CorruptDocumentError,
        DocumentError,
        PasswordRequiredError,
        PdfSession,
    )
    from offlinepdf.core.recovery import RecoveryStore, TempWorkspace

    tmp = TempWorkspace()
    sample = tmp.new_file("selftest", ".pdf")
    try:
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "OfflinePDF selftest", fontsize=16)
        page.insert_text((72, 130), "Fully offline by construction.", fontsize=11)
        sample.write_bytes(doc.tobytes())
        doc.close()
        result.check("sample pdf created", sample.exists(), f"{sample.stat().st_size} bytes")

        session = PdfSession.open(sample)
        result.check("open + page count", session.page_count == 1, f"{session.page_count} page(s)")

        png = session.render_pixmap(0, 1.0)
        result.check("render page 1", len(png) > 800, f"{len(png)} bytes png")

        out = tmp.new_file("selftest-out", ".pdf")
        session.save_as(out, overwrite=True)
        result.check("save_as + verify", out.exists() and session.verify_roundtrip())

        again = PdfSession.open(out)
        result.check("reopen saved file", again.page_count == session.page_count)
        again.close()

        # recovery snapshot (keyed to the document's current path)
        session.mark_dirty()
        result.check("recovery write", session.write_recovery())
        store = RecoveryStore()
        result.check("recovery read", store.read(out) is not None)
        session.clear_recovery()
        result.check("recovery cleared", store.read(out) is None)

        session.close()

        # corrupt file -> friendly error
        bad = tmp.new_file("selftest-bad", ".pdf")
        bad.write_bytes(b"this is definitely not a pdf")
        try:
            PdfSession.open(bad)
            result.check("corrupt file rejected", False, "no exception raised")
        except CorruptDocumentError:
            result.check("corrupt file rejected", True)

        # encrypted file -> password required, then open with password
        enc = tmp.new_file("selftest-enc", ".pdf")
        edoc = pymupdf.open()
        ep = edoc.new_page()
        ep.insert_text((72, 100), "secret")
        edoc.save(
            str(enc),
            encryption=getattr(pymupdf, "PDF_ENCRYPT_AES_256", 4),
            user_pw="hunter2",
            owner_pw="hunter2-owner",
        )
        edoc.close()
        try:
            PdfSession.open(enc)
            result.check("encrypted needs password", False, "opened without password")
        except PasswordRequiredError:
            result.check("encrypted needs password", True)
        try:
            es = PdfSession.open(enc, password="hunter2")
            ok = es.page_count == 1
            es.close()
            result.check("encrypted opens with password", ok)
        except DocumentError as exc:
            result.check("encrypted opens with password", False, str(exc))
    except Exception as exc:
        logger.exception("document selftest failed")
        result.check("document lifecycle", False, str(exc))

    # 4. Qt importable (no event loop).
    try:
        import PySide6.QtWidgets

        result.check("PySide6 import", True, PySide6.__version__)
    except Exception as exc:
        result.check("PySide6 import", False, str(exc))

    # 5. Temp wipe.
    result.check("temp wipe", tmp.wipe() >= 0 and not any(tmp.base.iterdir()))

    print("\n".join(result.lines))
    total = result.passed + result.failed
    print(f"\nselftest: {result.passed}/{total} passed, {result.failed} failed")
    if result.failed:
        print("SELFTEST FAILED", file=sys.stderr)
        return 1
    print("SELFTEST OK - application verified fully offline")
    return 0
