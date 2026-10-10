"""Shared HTTP client environment compatibility helpers."""

from __future__ import annotations

import os
import socket
from http.client import HTTPConnection, HTTPSConnection
from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock
from urllib.request import HTTPHandler, HTTPSHandler, Request, build_opener


def _connect_ipv4(address, timeout, source_address=None):
    host, port = address
    last_error = OSError(f"No IPv4 address found for {host}")
    for family, kind, protocol, _, target in socket.getaddrinfo(
        host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
    ):
        sock = socket.socket(family, kind, protocol)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(target)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error


class _IPv4ConnectionMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Change only TCP dialing; stdlib still owns CONNECT, TLS/SNI and verification.
        self._create_connection = _connect_ipv4


class _IPv4HTTPConnection(_IPv4ConnectionMixin, HTTPConnection):
    pass


class _IPv4HTTPSConnection(_IPv4ConnectionMixin, HTTPSConnection):
    pass


class _IPv4HTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(_IPv4HTTPConnection, request)


class _IPv4HTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_IPv4HTTPSConnection, request, context=self._context)


def ipv4_urlopen(request: Request, *, timeout: float):
    """Open HTTP(S) over IPv4, retaining environment proxies and TLS validation."""
    return build_opener(_IPv4HTTPHandler(), _IPv4HTTPSHandler()).open(
        request, timeout=timeout
    )


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_PROXY_ENV_LOCK = RLock()


@contextmanager
def without_unsupported_socks_proxy_env() -> Iterator[None]:
    """Hide generic socks:// fallbacks while preserving configured HTTP(S) proxies."""

    with _PROXY_ENV_LOCK:
        removed: dict[str, str] = {}
        for key in _PROXY_ENV_KEYS:
            value = os.environ.get(key)
            if isinstance(value, str) and value.lower().startswith("socks://"):
                removed[key] = value
                os.environ.pop(key, None)
        try:
            yield
        finally:
            os.environ.update(removed)
