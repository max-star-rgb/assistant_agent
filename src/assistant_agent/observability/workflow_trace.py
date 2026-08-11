"""Stable identities shared by foreground and durable Workflow projections."""

from hashlib import sha256


def workflow_root_span_id(export_trace_id: str) -> str:
    """Return the logical Plan-and-Execute root span for one export trace."""

    value = sha256(
        f"{export_trace_id}:deep_research.workflow.root".encode("utf-8")
    ).digest()[:8].hex()
    return value if int(value, 16) != 0 else "0000000000000001"
