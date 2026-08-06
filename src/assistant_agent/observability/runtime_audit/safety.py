"""Runtime-audit content and error safety helpers."""

from __future__ import annotations

import re

from assistant_agent.providers.provider_errors import sanitize_error_message


_URL_USERINFO = re.compile(
    r"([a-z][a-z0-9+.-]*://)[^/?#\s@]+@",
    flags=re.IGNORECASE,
)


def sanitize_runtime_audit_text(value: object) -> str:
    """Remove credentials, including URL userinfo, before audit persistence/output."""

    sanitized = sanitize_error_message(value)
    return _URL_USERINFO.sub(r"\1[redacted]@", sanitized)
