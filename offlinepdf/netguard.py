"""Network guard.

Installed at process startup before any other subsystem can open a socket.
Every attempt to reach the network (TCP/UDP connect, DNS lookup, urllib,
http.client) is logged locally and refused with :class:`NetworkBlockedError`.

The guard intentionally blocks *all* addresses, including loopback: the
application never needs a socket.  Qt's QtNetwork stack bypasses Python's
socket module and is instead prevented by the static source scan in
``scripts/check_offline.py``.
"""

from __future__ import annotations

import http.client
import logging
import socket
import threading
import urllib.request
from collections.abc import Callable
from typing import Any, NoReturn

__all__ = [
    "NetworkBlockedError",
    "attempts",
    "clear_attempts",
    "install",
    "is_installed",
    "uninstall",
]

logger = logging.getLogger("offlinepdf.netguard")

_MAX_ATTEMPTS = 1000

_lock = threading.Lock()
_installed = False
_originals: dict[str, Callable[..., Any]] = {}
_attempts: list[str] = []


class NetworkBlockedError(OSError):
    """Raised when code running inside the app attempts network access."""


def _record(kind: str, target: object) -> NoReturn:
    """Log a blocked attempt (bounded in-memory list + local log) and raise."""
    entry = f"{kind} -> {target!r}"
    with _lock:
        _attempts.append(entry)
        if len(_attempts) > _MAX_ATTEMPTS:
            del _attempts[0 : len(_attempts) - _MAX_ATTEMPTS]
    logger.warning("BLOCKED network attempt: %s", entry)
    raise NetworkBlockedError(
        f"OfflinePDF Studio blocks all network access (attempted: {kind})"
    )


# --- socket level patches -------------------------------------------------


def _blocked_connect(self: socket.socket, address: Any) -> NoReturn:
    _record("socket.connect", address)


def _blocked_connect_ex(self: socket.socket, address: Any) -> NoReturn:
    _record("socket.connect_ex", address)


def _blocked_sendto(self: socket.socket, data: bytes, *args: Any) -> NoReturn:
    _record("socket.sendto", args)


def _blocked_create_connection(*args: Any, **kwargs: Any) -> NoReturn:
    _record("socket.create_connection", args)


def _blocked_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> NoReturn:
    _record("socket.getaddrinfo", (host, port))


def _blocked_gethostbyname(host: str) -> NoReturn:
    _record("socket.gethostbyname", host)


def _blocked_gethostbyname_ex(host: str) -> NoReturn:
    _record("socket.gethostbyname_ex", host)


# --- application level patches (clearer errors, defence in depth) --------


def _blocked_urlopen(*args: Any, **kwargs: Any) -> NoReturn:
    _record("urllib.request.urlopen", args)


def _blocked_http_connect(self: Any) -> NoReturn:
    _record("http.client.connect", getattr(self, "host", "?"))


def _apply(container: object, name: str, replacement: object) -> None:
    key = f"{id(container)}.{name}"
    if key in _originals:
        return
    _originals[key] = getattr(container, name)  # type: ignore[assignment]
    setattr(container, name, replacement)


def _restore() -> None:
    for key, original in _originals.items():
        _, attr = key.rsplit(".", 1)
        container_id = int(key.split(".", 1)[0])
        container = _CONTAINERS.get(container_id)
        if container is not None:
            setattr(container, attr, original)
    _originals.clear()


_CONTAINERS: dict[int, object] = {}


def install() -> bool:
    """Install the guard. Idempotent. Returns True when newly installed."""
    global _installed
    with _lock:
        if _installed:
            return False
        _CONTAINERS[id(socket.socket)] = socket.socket
        _CONTAINERS[id(socket)] = socket
        _CONTAINERS[id(urllib.request)] = urllib.request
        _CONTAINERS[id(http.client.HTTPConnection)] = http.client.HTTPConnection
        # HTTPSConnection only exists when the ssl module is importable; the
        # frozen build may ship without it, in which case HTTPS is impossible.
        https_cls = getattr(http.client, "HTTPSConnection", None)
        if https_cls is not None:
            _CONTAINERS[id(https_cls)] = https_cls

        _apply(socket.socket, "connect", _blocked_connect)
        _apply(socket.socket, "connect_ex", _blocked_connect_ex)
        _apply(socket.socket, "sendto", _blocked_sendto)
        _apply(socket, "create_connection", _blocked_create_connection)
        _apply(socket, "getaddrinfo", _blocked_getaddrinfo)
        _apply(socket, "gethostbyname", _blocked_gethostbyname)
        _apply(socket, "gethostbyname_ex", _blocked_gethostbyname_ex)
        _apply(urllib.request, "urlopen", _blocked_urlopen)
        _apply(http.client.HTTPConnection, "connect", _blocked_http_connect)
        if https_cls is not None:
            _apply(https_cls, "connect", _blocked_http_connect)
        _installed = True
        logger.info("network guard installed (all outbound access blocked)")
        return True


def uninstall() -> bool:
    """Remove the guard and restore original functions. Used by tests."""
    global _installed
    with _lock:
        if not _installed:
            return False
        _restore()
        _installed = False
        logger.info("network guard removed")
        return True


def is_installed() -> bool:
    """Return True when the guard is active."""
    return _installed


def attempts() -> list[str]:
    """Return a copy of the recorded blocked-attempt log."""
    with _lock:
        return list(_attempts)


def clear_attempts() -> None:
    """Forget recorded attempts (used by tests and the selftest)."""
    with _lock:
        _attempts.clear()
