"""Context controls for bounded agent-to-agent delegation."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.schemas.agent_communication import AgentArtifact, AgentTask, AgentTaskResult
from assistant_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message


class OmittedDelegationContext(BaseModel):
    """One omitted metadata field and the reason it was not forwarded."""

    key: str
    reason: str


class ChildContextBudget(BaseModel):
    """Budget metadata attached to a child agent run."""

    token_budget: int | None = None
    tool_budget: int | None = None
    timeout_ms: int
    delegation_depth: int
    max_delegation_depth: int

    @classmethod
    def from_task(cls, task: AgentTask) -> "ChildContextBudget":
        return cls(
            token_budget=task.token_budget,
            tool_budget=task.tool_budget,
            timeout_ms=task.timeout_ms,
            delegation_depth=task.delegation_depth,
            max_delegation_depth=task.max_delegation_depth,
        )


class DelegationContextPack(BaseModel):
    """Filtered task plus trace metadata for the child context boundary."""

    task: AgentTask
    omitted_context: list[OmittedDelegationContext] = Field(default_factory=list)
    child_context_budget: ChildContextBudget
    tool_result_refs: list[dict[str, Any]] = Field(default_factory=list)

    def trace_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "child_context_budget": self.child_context_budget.model_dump(mode="json"),
            "agent_context": {
                "omitted_context": [item.model_dump(mode="json") for item in self.omitted_context],
                "omitted_context_count": len(self.omitted_context),
            },
        }
        if self.tool_result_refs:
            metadata["tool_result_refs"] = sanitize_error_detail(self.tool_result_refs)
        return metadata


class MemoryScopeFilter:
    """Prevent parent memory context payloads from crossing an agent boundary."""

    def is_memory_context_key(self, key: str) -> bool:
        normalized = _normalize_key(key)
        return normalized.startswith("memory_context") or normalized in {
            "memory",
            "memory_blocks",
            "memory_items",
            "memory_snapshot",
            "memory_summaries",
            "memory_text",
            "parent_memory",
            "retrieved_memories",
        }

    def metadata(self, task: AgentTask) -> dict[str, Any]:
        return {
            "identity_source": "agent_session_ref",
            "user_id": task.session.user_id,
            "session_id": task.session.session_id,
            "parent_memory_forwarded": False,
        }


class ToolResultPruner:
    """Convert raw parent tool results into small child-safe references."""

    def prune(self, value: Any) -> list[dict[str, Any]]:
        items = value if isinstance(value, list) else [value]
        refs: list[dict[str, Any]] = []
        for item in items[:20]:
            ref = self._ref_from_item(item)
            if ref is None:
                continue
            refs.append(ref)
        return refs

    def _ref_from_item(self, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        output_ref = item.get("output_ref")
        if not isinstance(output_ref, str) or not output_ref:
            output_refs = item.get("output_refs")
            if isinstance(output_refs, list):
                output_ref = next((ref for ref in output_refs if isinstance(ref, str) and ref), None)
        if not isinstance(output_ref, str) or not output_ref:
            artifact_ref = item.get("artifact_ref") or item.get("ref")
            output_ref = artifact_ref if isinstance(artifact_ref, str) and artifact_ref else None
        if output_ref is None:
            return None
        pruned: dict[str, Any] = {"output_ref": sanitize_error_message(output_ref)}
        for key in ("tool_name", "status", "capability"):
            value = item.get(key)
            if isinstance(value, str) and value:
                pruned[key] = sanitize_error_message(value)
        error = item.get("error")
        if isinstance(error, str) and error:
            pruned["error_summary"] = sanitize_error_message(error)
        return pruned


class ArtifactSummaryBuilder:
    """Build trace-safe summaries for delegated task artifacts."""

    def build(self, result: AgentTaskResult) -> dict[str, Any]:
        return {
            "artifact_count": len(result.artifacts),
            "kinds": sorted({artifact.kind for artifact in result.artifacts}),
            "output_ref_count": sum(len(artifact.output_refs) for artifact in result.artifacts),
            "text_chars": sum(len(artifact.text or "") for artifact in result.artifacts),
            "data_keys": _artifact_data_keys(result.artifacts),
        }


class DelegationContextBuilder:
    """Create a child-safe task envelope before transport dispatch."""

    _SAFE_MESSAGE_KEYS = frozenset(
        {
            "capability",
            "client_request_id",
            "context_refs",
            "locale",
            "request_origin",
            "source",
            "timezone",
        }
    )
    _SAFE_TASK_KEYS = frozenset(
        {
            "capability",
            "context_refs",
            "delegation_budget",
            "delegation_pairs",
            "request_origin",
            "routing_reason",
            "tool",
        }
    )
    _PARENT_CONTEXT_KEYS = frozenset(
        {
            "assistant_messages",
            "conversation",
            "conversation_history",
            "history",
            "messages",
            "parent_context",
            "parent_history",
            "prompt",
            "raw_prompt",
            "transcript",
        }
    )
    _RAW_OR_SECRET_MARKERS = (
        "api_key",
        "apikey",
        "authorization",
        "base64",
        "bearer",
        "cookie",
        "data_uri",
        "password",
        "provider_payload",
        "provider_response",
        "raw",
        "secret",
        "token",
    )

    def __init__(
        self,
        *,
        memory_scope_filter: MemoryScopeFilter | None = None,
        tool_result_pruner: ToolResultPruner | None = None,
        max_context_refs: int = 20,
    ) -> None:
        self.memory_scope_filter = memory_scope_filter or MemoryScopeFilter()
        self.tool_result_pruner = tool_result_pruner or ToolResultPruner()
        self.max_context_refs = max_context_refs

    def build(self, task: AgentTask) -> DelegationContextPack:
        message_metadata, message_omitted, message_tool_refs = self._filter_metadata(
            task.message.metadata,
            safe_keys=self._SAFE_MESSAGE_KEYS,
        )
        task_metadata, task_omitted, task_tool_refs = self._filter_metadata(
            task.metadata,
            safe_keys=self._SAFE_TASK_KEYS,
        )
        omitted = message_omitted + task_omitted
        tool_result_refs = message_tool_refs + task_tool_refs
        context_refs = _dedupe_strings(
            [
                *_list_strings(message_metadata.get("context_refs")),
                *_list_strings(task_metadata.get("context_refs")),
            ],
            limit=self.max_context_refs,
        )
        if context_refs:
            message_metadata["context_refs"] = context_refs
            task_metadata["context_refs"] = context_refs

        child_context_budget = ChildContextBudget.from_task(task)
        agent_context = {
            "context_refs": context_refs,
            "memory_scope": self.memory_scope_filter.metadata(task),
            "omitted_context": [item.model_dump(mode="json") for item in omitted],
            "omitted_context_count": len(omitted),
        }
        if tool_result_refs:
            agent_context["tool_result_refs"] = sanitize_error_detail(tool_result_refs)
            task_metadata["tool_result_refs"] = sanitize_error_detail(tool_result_refs)
        task_metadata["agent_context"] = agent_context
        task_metadata["child_context_budget"] = child_context_budget.model_dump(mode="json")

        message = task.message.model_copy(update={"metadata": message_metadata}, deep=True)
        filtered_task = task.model_copy(
            update={"message": message, "metadata": task_metadata},
            deep=True,
        )
        return DelegationContextPack(
            task=filtered_task,
            omitted_context=omitted,
            child_context_budget=child_context_budget,
            tool_result_refs=tool_result_refs,
        )

    def _filter_metadata(
        self,
        metadata: dict[str, Any],
        *,
        safe_keys: frozenset[str],
    ) -> tuple[dict[str, Any], list[OmittedDelegationContext], list[dict[str, Any]]]:
        filtered: dict[str, Any] = {}
        omitted: list[OmittedDelegationContext] = []
        tool_result_refs: list[dict[str, Any]] = []
        for key, value in metadata.items():
            reason = self._omit_reason(key, value, safe_keys=safe_keys)
            if reason is not None:
                omitted.append(OmittedDelegationContext(key=_context_key_name(key), reason=reason))
                if reason == "tool_result_pruned":
                    tool_result_refs.extend(self.tool_result_pruner.prune(value))
                continue
            filtered[key] = sanitize_error_detail(value)
        return filtered, omitted, tool_result_refs

    def _omit_reason(self, key: str, value: Any, *, safe_keys: frozenset[str]) -> str | None:
        normalized = _normalize_key(key)
        if self.memory_scope_filter.is_memory_context_key(key):
            return "memory_context_not_forwarded"
        if normalized in {"tool_result", "tool_results", "tool_observation", "tool_observations"}:
            return "tool_result_pruned"
        if normalized in self._PARENT_CONTEXT_KEYS or normalized.endswith("_history"):
            return "parent_context_not_forwarded"
        if _has_raw_or_secret_marker(normalized, self._RAW_OR_SECRET_MARKERS):
            return "raw_or_secret_payload_not_forwarded"
        if _is_large_text(value):
            return "large_value_not_forwarded"
        if key not in safe_keys:
            return "not_allowlisted"
        return None


def _artifact_data_keys(artifacts: list[AgentArtifact]) -> list[str]:
    keys = set()
    for artifact in artifacts:
        keys.update(str(key) for key in artifact.data)
    return sorted(keys)


def _dedupe_strings(values: list[str], *, limit: int) -> list[str]:
    seen = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
        if len(deduped) >= limit:
            break
    return deduped


def _list_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [sanitize_error_message(item) for item in value if isinstance(item, str) and item]


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _context_key_name(key: str) -> str:
    text = " ".join(str(key).strip().split())
    normalized = _normalize_key(text)
    secret_key_names = {
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "cookie",
        "secret",
        "token",
        "password",
    }
    if normalized in secret_key_names:
        return normalized.replace("_", "-")
    if len(text) <= 100:
        return text
    return f"{text[:97]}..."


def _has_raw_or_secret_marker(normalized_key: str, markers: tuple[str, ...]) -> bool:
    for marker in markers:
        if marker == "raw":
            if normalized_key == "raw" or normalized_key.startswith("raw_") or "_raw_" in normalized_key:
                return True
            continue
        if marker in normalized_key:
            return True
    return False


def _is_large_text(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 1_000
