"""Shared HTTP client environment compatibility helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock


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
