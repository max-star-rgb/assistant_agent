from __future__ import annotations

import socket

import pytest

from assistant_agent.tools.plugins.builtin.website_guidance.url_policy import (
    WebUrlValidationError,
    validate_public_web_url,
)


def _resolver(*addresses: str):
    def resolve(host: str, port: int, **kwargs: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_UNSPEC, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return resolve


def _error_code(url: str, resolver) -> str:
    with pytest.raises(WebUrlValidationError) as raised:
        validate_public_web_url(url, resolver=resolver)
    return raised.value.code


def test_validate_public_web_url_accepts_https_url_with_global_resolution() -> None:
    target = validate_public_web_url(
        "https://public.example/path?step=1",
        resolver=_resolver("93.184.216.34"),
    )

    assert target.url == "https://public.example/path?step=1"
    assert target.host == "public.example"
    assert target.port == 443
    assert target.resolved_addresses == ("93.184.216.34",)


@pytest.mark.parametrize(
    ("url", "expected_code"),
    [
        ("file:///etc/passwd", "unsafe_url"),
        ("https://user:password@public.example/", "unsafe_url"),
        ("https://localhost/", "unsafe_url"),
        ("ftp://public.example/", "unsafe_url"),
        ("https://public.example:0/", "unsafe_url"),
        ("https://public.example:8443/", "unsafe_url"),
    ],
)
def test_validate_public_web_url_rejects_unsafe_url_forms(
    url: str,
    expected_code: str,
) -> None:
    assert _error_code(url, _resolver("93.184.216.34")) == expected_code


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.7",
        "169.254.1.1",
        "fc00::1",
        "::1",
        "fe80::1",
    ],
)
def test_validate_public_web_url_rejects_non_global_resolved_addresses(
    address: str,
) -> None:
    assert (
        _error_code("https://public.example/", _resolver(address))
        == "unsafe_resolved_address"
    )


def test_validate_public_web_url_rejects_empty_or_mixed_resolution() -> None:
    assert _error_code("https://public.example/", _resolver()) == "unsafe_resolved_address"
    assert (
        _error_code(
            "https://public.example/",
            _resolver("93.184.216.34", "10.0.0.7"),
        )
        == "unsafe_resolved_address"
    )


@pytest.mark.parametrize("url", ["https://127.0.0.1/", "https://[::1]/"])
def test_validate_public_web_url_rejects_non_global_ip_literals_before_resolution(
    url: str,
) -> None:
    assert _error_code(url, _resolver("93.184.216.34")) == "unsafe_resolved_address"


def test_validate_public_web_url_treats_resolver_failures_as_unsafe() -> None:
    def failing_resolver(host: str, port: int, **kwargs: object) -> list[tuple[object, ...]]:
        raise RuntimeError("resolver unavailable")

    assert (
        _error_code("https://public.example/", failing_resolver)
        == "unsafe_resolved_address"
    )
