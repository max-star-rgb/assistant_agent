"""Fail-closed validation for public HTTP(S) website targets."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from collections.abc import Callable, Sequence
from urllib.parse import SplitResult, urlsplit


_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_PORTS = frozenset({80, 443})


class WebUrlValidationError(ValueError):
    """A stable failure response for rejected browser navigation targets."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ValidatedWebTarget:
    """A public web target whose complete resolution was checked."""

    url: str
    host: str
    port: int
    resolved_addresses: tuple[str, ...]


Resolver = Callable[..., Sequence[tuple[object, ...]]]


def validate_public_web_url(
    url: str,
    resolver: Resolver = socket.getaddrinfo,
) -> ValidatedWebTarget:
    """Validate a browser URL, rejecting every non-public resolution outcome."""

    parsed = _safe_split(url)
    host = parsed.hostname
    if (
        parsed.scheme.lower() not in _ALLOWED_SCHEMES
        or parsed.username is not None
        or parsed.password is not None
        or host is None
        or not host
        or host.rstrip(".").lower() == "localhost"
    ):
        raise WebUrlValidationError("unsafe_url")

    try:
        port = parsed.port
    except ValueError as error:
        raise WebUrlValidationError("unsafe_url") from error
    effective_port = port or _default_port(parsed)
    if effective_port not in _ALLOWED_PORTS:
        raise WebUrlValidationError("unsafe_url")
    if _is_ip_literal(host) and not _is_public_address(host):
        raise WebUrlValidationError("unsafe_resolved_address")

    addresses = _resolve_all(host, effective_port, resolver)
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise WebUrlValidationError("unsafe_resolved_address")

    return ValidatedWebTarget(
        url=url,
        host=host,
        port=effective_port,
        resolved_addresses=tuple(sorted(addresses)),
    )


def _safe_split(url: str) -> SplitResult:
    if not isinstance(url, str) or not url or url != url.strip():
        raise WebUrlValidationError("unsafe_url")
    try:
        return urlsplit(url)
    except ValueError as error:
        raise WebUrlValidationError("unsafe_url") from error


def _default_port(parsed: SplitResult) -> int:
    return 443 if parsed.scheme.lower() == "https" else 80


def _resolve_all(host: str, port: int, resolver: Resolver) -> set[str]:
    try:
        results = resolver(host, port, type=socket.SOCK_STREAM)
    except Exception:
        return set()

    addresses: set[str] = set()
    try:
        for result in results:
            sockaddr = result[4]
            address = sockaddr[0]
            if not isinstance(address, str):
                return set()
            addresses.add(address)
    except (IndexError, TypeError):
        return set()
    return addresses


def _is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_global and not any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
