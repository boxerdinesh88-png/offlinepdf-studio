"""The network guard must block every outbound path and stay installed."""

from __future__ import annotations

import http.client
import socket
import urllib.request

import pytest

from offlinepdf import netguard


def test_guard_is_installed():
    assert netguard.is_installed() is True


def test_install_is_idempotent():
    assert netguard.install() is False  # already installed by conftest


@pytest.mark.parametrize(
    "fn",
    [
        lambda: socket.getaddrinfo("example.com", 443),
        lambda: socket.gethostbyname("example.com"),
        lambda: socket.create_connection(("example.com", 443), timeout=1),
        lambda: urllib.request.urlopen("https://example.com", timeout=1),
        lambda: socket.socket().connect(("example.com", 443)),
    ],
)
def test_outbound_attempts_blocked(fn):
    netguard.clear_attempts()
    with pytest.raises(netguard.NetworkBlockedError):
        fn()
    assert netguard.attempts()


def test_blocked_error_is_oserror():
    assert issubclass(netguard.NetworkBlockedError, OSError)


def test_http_client_connect_blocked():
    conn = http.client.HTTPConnection("example.com", 80, timeout=1)
    with pytest.raises(netguard.NetworkBlockedError):
        conn.connect()
    conn.close()


def test_attempts_are_recorded_and_clearable():
    netguard.clear_attempts()
    assert netguard.attempts() == []
    with pytest.raises(netguard.NetworkBlockedError):
        socket.gethostbyname("example.org")
    log = netguard.attempts()
    assert len(log) == 1 and "gethostbyname" in log[0]
    netguard.clear_attempts()
    assert netguard.attempts() == []
