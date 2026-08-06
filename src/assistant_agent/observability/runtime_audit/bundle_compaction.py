"""Pure compaction for persisted runtime audit trace evidence."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from assistant_agent.observability.runtime_audit.models import (
    LangfuseTraceSnapshot,
)


def compact_trace_evidence(
    traces: list[LangfuseTraceSnapshot],
) -> tuple[list[LangfuseTraceSnapshot], dict[str, list[Any]]]:
    """Remove raw metadata and deduplicate Tool catalogs without mutating input."""

    catalogs: dict[str, list[Any]] = {}
    catalog_payloads: dict[str, bytes] = {}
    compacted: list[LangfuseTraceSnapshot] = []
    for trace in traces:
        observations = [
            observation.model_copy(
                update={
                    "input": _replace_input_tool_catalogs(
                        observation.input,
                        catalogs=catalogs,
                        catalog_payloads=catalog_payloads,
                    ),
                    "metadata": None,
                },
                deep=True,
            )
            for observation in trace.observations
        ]
        scores = [
            score.model_copy(update={"metadata": None}, deep=True)
            for score in trace.scores
        ]
        compacted.append(
            trace.model_copy(
                update={
                    "input": _replace_input_tool_catalogs(
                        trace.input,
                        catalogs=catalogs,
                        catalog_payloads=catalog_payloads,
                    ),
                    "metadata": None,
                    "observations": observations,
                    "scores": scores,
                },
                deep=True,
            )
        )
    return compacted, catalogs


def _replace_input_tool_catalogs(
    value: Any,
    *,
    catalogs: dict[str, list[Any]],
    catalog_payloads: dict[str, bytes],
) -> Any:
    copied = deepcopy(value)
    if isinstance(copied, list):
        return [
            _replace_input_tool_catalogs(
                item,
                catalogs=catalogs,
                catalog_payloads=catalog_payloads,
            )
            for item in copied
        ]
    if not isinstance(copied, dict):
        return copied
    if isinstance(copied.get("tools"), list) and "tool_catalog_ref" in copied:
        raise ValueError("Tool input cannot contain both tools and tool_catalog_ref")
    result: dict[Any, Any] = {}
    for key, item in copied.items():
        if key == "tools" and isinstance(item, list):
            payload = _canonical_catalog_bytes(item)
            catalog_id = hashlib.sha256(payload).hexdigest()
            existing_payload = catalog_payloads.get(catalog_id)
            if existing_payload is not None and existing_payload != payload:
                raise ValueError("Tool catalog digest collision")
            catalog_payloads[catalog_id] = payload
            catalogs.setdefault(catalog_id, item)
            result["tool_catalog_ref"] = catalog_id
            continue
        result[key] = _replace_input_tool_catalogs(
            item,
            catalogs=catalogs,
            catalog_payloads=catalog_payloads,
        )
    return result


def _canonical_catalog_bytes(value: list[Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Tool catalog must be JSON serializable") from exc
