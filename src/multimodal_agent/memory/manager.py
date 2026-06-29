"""Memory manager boundary for layered agent memory access."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from multimodal_agent.memory.profile import USER_PROFILE_MEMORY_ID, UserProfileMemory
from multimodal_agent.memory.store import MemoryStore
from multimodal_agent.memory.write_policy import (
    MemoryWritePolicy,
    build_explicit_memory_item,
    build_memory_item_from_promotion_candidate,
    build_run_summary_promotion_candidate,
    promotion_decision_audit_record,
)
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery, MemorySearchResult
from multimodal_agent.schemas.requests import UserRequest


MemoryLayer = Literal["session", "semantic", "episodic", "artifact", "procedural"]


class MemoryContextBlock(BaseModel):
    """A prompt-safe grouped view of retrieved memories."""

    layer: MemoryLayer
    title: str
    items: list[MemoryItem] = Field(default_factory=list)


class MemoryContext(BaseModel):
    """Structured memory context loaded for one agent run."""

    items: list[MemoryItem] = Field(default_factory=list)
    text: str = ""
    summaries: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    blocks: list[MemoryContextBlock] = Field(default_factory=list)


class MemoryManager:
    """Coordinate memory retrieval, write policy, and context formatting.

    Agent nodes and tools should depend on this boundary instead of reaching
    directly into stores, retrievers, and context builders.
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        write_policy: MemoryWritePolicy | None = None,
        default_top_k: int = 5,
        default_max_context_chars: int = 500,
    ) -> None:
        self.store = store
        self.write_policy = write_policy or MemoryWritePolicy()
        self.default_top_k = default_top_k
        self.default_max_context_chars = default_max_context_chars

    def search(self, query: MemoryQuery) -> MemorySearchResult:
        """Search through the configured store."""

        return self.store.search(query)

    def load_context_for_request(
        self,
        request: UserRequest,
        *,
        capability: str | None = None,
        top_k: int | None = None,
        max_context_chars: int | None = None,
    ) -> MemoryContext:
        """Load bounded, layered memory context for a user request."""

        query = MemoryQuery(
            user_id=request.user_id,
            query=request.text or "",
            capability=capability,
            top_k=top_k or self.default_top_k,
            max_context_chars=max_context_chars or self.default_max_context_chars,
        )
        result = self.search(query)
        return self.build_context(result.items, max_chars=query.max_context_chars)

    def load_into_state(
        self,
        state: Any,
        request: UserRequest,
        *,
        capability: str | None = None,
        top_k: int | None = None,
        max_context_chars: int | None = None,
    ) -> MemoryContext:
        """Load memory and attach prompt-safe metadata to AgentState."""

        context = self.load_context_for_request(
            request,
            capability=capability,
            top_k=top_k,
            max_context_chars=max_context_chars,
        )
        state.memory_context = context.items
        state.request.metadata["memory_context_text"] = context.text
        state.request.metadata["memory_context_summaries"] = context.summaries
        state.request.metadata["memory_context_refs"] = context.artifact_refs
        state.request.metadata["memory_context_blocks"] = [
            block.model_dump(mode="json") for block in context.blocks
        ]
        return context

    def build_context(self, items: list[MemoryItem], *, max_chars: int | None = None) -> MemoryContext:
        """Build grouped context text while preserving the retrieved items."""

        blocks = _group_by_layer(items)
        summaries = [item.summary for item in items]
        artifact_refs = [ref for item in items for ref in item.artifact_refs]
        text = format_layered_memory_context(blocks, max_chars=max_chars or self.default_max_context_chars)
        return MemoryContext(
            items=items,
            text=text,
            summaries=summaries,
            artifact_refs=artifact_refs,
            blocks=blocks,
        )

    def save_from_run(self, state: Any) -> MemoryItem | None:
        """Evaluate a completed-run memory candidate and persist only when policy allows."""

        if state.status != "completed" or state.response is None:
            return None
        if _is_pure_memory_save_run(state):
            return None
        output_refs = [
            ref
            for result in state.tool_results
            for ref in ([result.output_ref] if result.output_ref else [])
        ]
        candidate = build_run_summary_promotion_candidate(
            user_id=state.user_id,
            session_id=state.session_id,
            summary=state.response.message if state.response else "Agent run completed.",
            intent=state.intent.intent if state.intent else None,
            selected_tools=[tool.tool_name for tool in state.selected_tools],
            output_refs=output_refs,
            policy=self.write_policy,
        )
        if candidate is None:
            _record_no_promotion_candidate(state, "auto_save_task_summary_disabled")
            return None
        decision = self.write_policy.evaluate_promotion_candidate(candidate)
        item = build_memory_item_from_promotion_candidate(
            memory_id=f"run_memory_{uuid4().hex}",
            candidate=candidate,
            policy=self.write_policy,
            created_at=datetime.now(timezone.utc),
        )
        saved = self.store.save(item) if item is not None else None
        _record_promotion_decision(state, decision, saved)
        return saved

    def save_explicit(
        self,
        *,
        user_id: str,
        session_id: str,
        text: str,
        content: dict[str, Any] | None = None,
        memory_id: str | None = None,
        created_at: datetime | None = None,
    ) -> MemoryItem:
        """Persist an explicit user-requested memory."""

        item = build_explicit_memory_item(
            memory_id=memory_id or f"explicit_memory_{uuid4().hex}",
            user_id=user_id,
            session_id=session_id,
            text=text,
            content=content,
            policy=self.write_policy,
            created_at=created_at,
        )
        saved = self._merge_or_save(item)
        self._upsert_user_profile(saved)
        return saved

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        return self.store.get(user_id, memory_id)

    def list_by_user(self, user_id: str) -> list[MemoryItem]:
        return self.store.list_by_user(user_id)

    def delete(self, user_id: str, memory_id: str) -> bool:
        return self.store.delete(user_id, memory_id)

    def delete_by_session(self, user_id: str, session_id: str) -> int:
        return self.store.delete_by_session(user_id, session_id)

    def clear_user(self, user_id: str) -> None:
        self.store.clear_user(user_id)

    def _merge_or_save(self, item: MemoryItem) -> MemoryItem:
        duplicate = self._find_duplicate(item)
        if duplicate is None:
            return self.store.save(item)

        observation_count = _observation_count(duplicate) + 1
        merged = duplicate.model_copy(
            update={
                "session_id": duplicate.session_id or item.session_id,
                "content": {
                    **duplicate.content,
                    **item.content,
                    "observation_count": observation_count,
                },
                "tags": _unique([*duplicate.tags, *item.tags]),
                "artifact_refs": _unique([*duplicate.artifact_refs, *item.artifact_refs]),
                "updated_at": item.created_at,
                "sensitivity": _merged_sensitivity(duplicate.sensitivity, item.sensitivity),
            }
        )
        return self.store.save(merged)

    def _find_duplicate(self, item: MemoryItem) -> MemoryItem | None:
        item_key = _dedupe_key(item)
        if not item_key:
            return None
        for existing in self.store.list_by_user(item.user_id):
            if existing.memory_id == item.memory_id or existing.source == "user_profile":
                continue
            if existing.memory_type != item.memory_type:
                continue
            if _dedupe_key(existing) == item_key:
                return existing
        return None

    def _upsert_user_profile(self, item: MemoryItem) -> MemoryItem | None:
        if item.source == "user_profile" or item.memory_type not in {"preference", "product", "task"}:
            return None

        existing = self.store.get(item.user_id, USER_PROFILE_MEMORY_ID)
        profile = (
            UserProfileMemory.from_memory_item(existing)
            if existing is not None
            else UserProfileMemory.empty(item.user_id, now=item.created_at)
        )
        changed = profile.merge_memory(item, now=item.updated_at or item.created_at)
        if not changed and existing is not None:
            return existing

        profile_item = profile.to_memory_item(session_id=item.session_id)
        if existing is not None:
            profile_item = profile_item.model_copy(update={"created_at": existing.created_at})
        return self.store.save(profile_item)


def format_layered_memory_context(blocks: list[MemoryContextBlock], max_chars: int = 500) -> str:
    """Format memory blocks into a bounded prompt-safe context string."""

    if not blocks:
        return ""

    lines = ["相关历史："]
    for block in blocks:
        candidate = "\n".join(lines + [block.title])
        if len(candidate) > max_chars:
            break
        lines.append(block.title)
        for item in block.items:
            ref_text = f" 引用：{item.artifact_refs[0]}" if item.artifact_refs else ""
            line = f"- [{item.memory_type}] {item.summary}{ref_text}"
            candidate = "\n".join(lines + [line])
            if len(candidate) > max_chars:
                break
            lines.append(line)

    return "\n".join(lines)[:max_chars]


def _group_by_layer(items: list[MemoryItem]) -> list[MemoryContextBlock]:
    grouped: dict[MemoryLayer, list[MemoryItem]] = {
        "semantic": [],
        "session": [],
        "episodic": [],
        "artifact": [],
        "procedural": [],
    }
    for item in items:
        grouped[_layer_for(item)].append(item)

    blocks: list[MemoryContextBlock] = []
    for layer, title in _LAYER_TITLES:
        layer_items = grouped[layer]
        if layer_items:
            blocks.append(MemoryContextBlock(layer=layer, title=title, items=layer_items))
    return blocks


def _layer_for(item: MemoryItem) -> MemoryLayer:
    if item.memory_type == "preference":
        return "semantic"
    if item.memory_type == "conversation":
        return "session"
    if item.memory_type == "task":
        return "episodic"
    return "artifact"


def _is_pure_memory_save_run(state: Any) -> bool:
    successful_tool_names = {
        result.tool_name
        for result in getattr(state, "tool_results", [])
        if getattr(result, "success", False)
    }
    return successful_tool_names == {"memory_save"}


def _record_no_promotion_candidate(state: Any, reason: str) -> None:
    metadata = getattr(getattr(state, "request", None), "metadata", None)
    if not isinstance(metadata, dict):
        return
    metadata["auto_task_summary_memory"] = {
        "skipped": True,
        "reason": reason,
        "candidate": False,
    }


def _record_promotion_decision(state: Any, decision: Any, saved: MemoryItem | None) -> None:
    metadata = getattr(getattr(state, "request", None), "metadata", None)
    if not isinstance(metadata, dict):
        return
    _increment_metadata_count(metadata, "memory_promotion_candidates")
    if saved is not None:
        _increment_metadata_count(metadata, "memory_promotion_written")
    else:
        _increment_metadata_count(metadata, "memory_promotion_rejected")
    audit = metadata.setdefault("memory_promotion_candidate_audit", [])
    if isinstance(audit, list):
        audit.append(promotion_decision_audit_record(decision, written_memory_id=saved.memory_id if saved else None))
        del audit[:-10]
    metadata["auto_task_summary_memory"] = {
        "skipped": saved is None,
        "reason": decision.reason,
        "candidate": True,
        "written": saved is not None,
        "memory_id": saved.memory_id if saved else None,
    }


def _increment_metadata_count(metadata: dict[str, Any], key: str) -> None:
    value = metadata.get(key)
    metadata[key] = value + 1 if isinstance(value, int) and value >= 0 else 1


_LAYER_TITLES: list[tuple[MemoryLayer, str]] = [
    ("semantic", "偏好/事实记忆："),
    ("session", "长期化对话："),
    ("episodic", "任务/经历记忆："),
    ("artifact", "产物/对象引用："),
    ("procedural", "过程/规则记忆："),
]


def _dedupe_key(item: MemoryItem) -> str:
    return _normalize_for_dedupe(item.summary)


def _normalize_for_dedupe(value: str) -> str:
    return "".join(ch for ch in value.strip().lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _observation_count(item: MemoryItem) -> int:
    value = item.content.get("observation_count")
    if isinstance(value, int) and value >= 1:
        return value
    return 1


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _merged_sensitivity(left: str, right: str) -> str:
    order = {"normal": 0, "private": 1, "sensitive": 2}
    return left if order.get(left, 0) >= order.get(right, 0) else right
