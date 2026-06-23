"""Shared assistant run backend for CLI, HTTP, and WebSocket entrypoints."""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.agent.state import AgentState
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.api import AgentRunResponse, agent_run_response_from_state
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.event_sink import EventSink, ListEventSink
from multimodal_agent.services.trace_store import trace_debug_summary
from multimodal_agent.services.video_context import load_demo_video_frames


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_MAX_HISTORY_TURNS = 8


@dataclass(frozen=True)
class ConversationTurn:
    """One completed user/assistant exchange in a session."""

    user_text: str
    assistant_text: str
    run_id: str
    trace_id: str

    def model_dump(self) -> dict[str, str]:
        return {
            "user_text": self.user_text,
            "assistant_text": self.assistant_text,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
        }


class InMemoryConversationStore:
    """Small process-local conversation store keyed by user/session."""

    def __init__(self, *, max_turns: int = DEFAULT_MAX_HISTORY_TURNS) -> None:
        self.max_turns = max_turns
        self._turns: dict[tuple[str, str], list[ConversationTurn]] = {}

    def get(self, user_id: str, session_id: str) -> list[ConversationTurn]:
        return list(self._turns.get((user_id, session_id), []))

    def append(self, user_id: str, session_id: str, turn: ConversationTurn) -> None:
        key = (user_id, session_id)
        turns = [*self._turns.get(key, []), turn]
        self._turns[key] = turns[-self.max_turns :]

    def clear(self, user_id: str, session_id: str) -> None:
        self._turns.pop((user_id, session_id), None)

    def clear_user(self, user_id: str) -> int:
        keys = [key for key in self._turns if key[0] == user_id]
        for key in keys:
            self._turns.pop(key, None)
        return len(keys)


_DEFAULT_CONVERSATION_STORE = InMemoryConversationStore()


@dataclass
class AssistantRunArtifacts:
    """Runtime artifacts shared by CLI and API layers."""

    runtime: AgentGraphRuntime
    state: AgentState
    events: list[Any]

    @property
    def runtime_info(self) -> dict[str, Any]:
        config = getattr(self.runtime, "config", None)
        if config is None:
            config = ProviderConfig.from_env({})
        return runtime_info(config)

    @property
    def current_stage(self) -> str:
        return current_stage(self.state)

    @property
    def blocked_reason(self) -> str | None:
        return blocked_reason(self.state)

    def api_response(self) -> AgentRunResponse:
        return agent_run_response_from_state(
            self.state,
            runtime_info=self.runtime_info,
            current_stage=self.current_stage,
            blocked_reason=self.blocked_reason,
        )

    def cli_payload(self) -> dict[str, Any]:
        response = self.api_response()
        trace_summary = trace_debug_summary(self.runtime.trace_store.list_by_run(self.state.run_id))
        return {
            "status": "success" if self.state.status != "failed" else "failed",
            "provider": self.runtime.config.chat_provider,
            "model": self.runtime.config.chat_model,
            "runtime_profile": self.runtime.config.runtime_profile.name,
            "graph_mode": self.runtime.config.agent_graph_mode,
            "query": self.state.request.text or "",
            "response_text": response.response_text,
            "response_data": response.data,
            "tool_sequence": [call["tool_name"] for call in response.tool_calls],
            "tool_calls": [
                {
                    "tool_name": call.get("tool_name"),
                    "status": call.get("status"),
                    "output_ref": call.get("output_ref"),
                    "error": call.get("error_message"),
                }
                for call in response.tool_calls
            ],
            "tool_results": response.tool_results,
            "react_steps": response.react_steps,
            "decision_trace": response.decision_trace,
            "events": [event.model_dump(mode="json", exclude_none=True) for event in self.events],
            "trace": trace_summary,
            "errors": [error.model_dump(mode="json") for error in response.errors],
            "run_id": response.run_id,
            "trace_id": response.trace_id,
            "runtime_info": response.runtime_info,
            "current_stage": response.current_stage,
            "blocked_reason": response.blocked_reason,
        }


def load_env_file(path: Path | str = DEFAULT_ENV_FILE, *, override: bool = False) -> dict[str, str]:
    """Load dotenv-style KEY=VALUE pairs without adding a dependency."""

    env_path = Path(path)
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if not key:
            continue
        loaded[key] = _strip_env_value(value.strip())
        if override or key not in os.environ:
            os.environ[key] = loaded[key]
    return loaded


def create_runtime(
    *,
    config: ProviderConfig | None = None,
    event_sink: EventSink | None = None,
    load_env: bool = True,
) -> AgentGraphRuntime:
    """Create the shared runtime with manual `.env` loading and offline test isolation."""

    if _is_pytest():
        resolved_config = config or ProviderConfig.from_env({})
    else:
        if load_env:
            load_env_file()
        resolved_config = config or ProviderConfig.from_env()
    return AgentGraphRuntime(config=resolved_config, event_sink=event_sink)


def run_assistant_request(
    request: UserRequest,
    *,
    config: ProviderConfig | None = None,
    event_sink: EventSink | None = None,
    runtime: AgentGraphRuntime | None = None,
    load_env: bool = True,
    conversation_store: InMemoryConversationStore | None = None,
    enable_conversation_history: bool = True,
) -> AssistantRunArtifacts:
    """Run one request and return shared artifacts."""

    sink = event_sink or ListEventSink()
    resolved_runtime = runtime or create_runtime(config=config, event_sink=sink, load_env=load_env)
    resolved_store = conversation_store or _DEFAULT_CONVERSATION_STORE
    _preload_demo_video_context(request, resolved_runtime)
    resolved_request = _prepare_conversation_request(
        request,
        conversation_store=resolved_store,
        enable_conversation_history=enable_conversation_history,
    )
    state = _run_state_with_sink(resolved_runtime, resolved_request, sink)
    _record_conversation_turn(
        state,
        conversation_store=resolved_store,
        enable_conversation_history=enable_conversation_history,
    )
    raw_events = getattr(sink, "events", [])
    events = list(raw_events) if isinstance(raw_events, list) else []
    return AssistantRunArtifacts(runtime=resolved_runtime, state=state, events=events)


def _run_state_with_sink(runtime: AgentGraphRuntime, request: UserRequest, sink: EventSink) -> AgentState:
    """Call run_state with a per-run sink, tolerating runtimes/test doubles that omit the param."""

    try:
        accepts_sink = "event_sink" in inspect.signature(runtime.run_state).parameters
    except (TypeError, ValueError):
        accepts_sink = False
    if accepts_sink:
        return runtime.run_state(request, event_sink=sink)
    return runtime.run_state(request)


def _preload_demo_video_context(request: UserRequest, runtime: AgentGraphRuntime) -> None:
    video_context_store = getattr(runtime, "video_context_store", None)
    if video_context_store is None:
        return
    for video_id in request.video_ids:
        if isinstance(video_id, str) and video_id:
            load_demo_video_frames(video_context_store, video_id)


def run_assistant_query(
    query: str,
    *,
    image_refs: list[str] | None = None,
    video_refs: list[str] | None = None,
    user_id: str = "demo_user",
    session_id: str = "demo_session",
    config: ProviderConfig | None = None,
    event_sink: EventSink | None = None,
    load_env: bool = True,
    metadata: dict[str, Any] | None = None,
    conversation_store: InMemoryConversationStore | None = None,
    enable_conversation_history: bool = True,
) -> AssistantRunArtifacts:
    """Run a text query through the shared assistant backend."""

    request = UserRequest(
        user_id=user_id,
        session_id=session_id,
        text=query,
        image_ids=list(image_refs or []),
        video_ids=list(video_refs or []),
        metadata=metadata or {"source": "assistant_run_service"},
    )
    return run_assistant_request(
        request,
        config=config,
        event_sink=event_sink,
        load_env=load_env,
        conversation_store=conversation_store,
        enable_conversation_history=enable_conversation_history,
    )


def runtime_info(config: ProviderConfig) -> dict[str, Any]:
    """Return redacted runtime/provider information."""

    return {
        "runtime_profile": config.runtime_profile.name,
        "graph_mode": config.agent_graph_mode,
        "providers": {
            "chat": config.chat_provider,
            "vision": config.vision_provider,
            "product_search": config.product_search_provider,
            "price_compare": config.price_compare_provider,
            "image_generation": config.image_generation_provider,
            "render": config.render_provider,
            "video": config.video_provider,
        },
        "offline_default": not config.runtime_profile.allows_real_providers,
    }


def current_stage(state: AgentState) -> str:
    """Describe where the assistant stopped or completed."""

    if state.status == "failed":
        return "failed"
    steps = _safe_steps(state.request.metadata.get("assistant_loop_steps"))
    if not steps:
        return "final_response" if state.response else "not_started"
    last = steps[-1]
    if last.get("observation_tool"):
        return "observation_received"
    decision_type = last.get("decision_type")
    if decision_type == "tool_call":
        return "tool_selected"
    if decision_type == "ask_followup":
        return "waiting_for_user"
    if decision_type == "final_answer":
        return "final_answer"
    return "assistant_decision"


def blocked_reason(state: AgentState) -> str | None:
    """Return a compact reason when the run stopped at an error or limit."""

    if state.errors:
        return state.errors[-1].message
    steps = _safe_steps(state.request.metadata.get("assistant_loop_steps"))
    if not steps:
        return None
    last = steps[-1]
    reason = str(last.get("reason") or "")
    if "最大工具调用次数" in reason or "工具调用上限" in reason:
        return reason
    if last.get("error"):
        return str(last["error"])
    return None


def clear_conversation_history(
    user_id: str,
    session_id: str,
    *,
    conversation_store: InMemoryConversationStore | None = None,
) -> None:
    """Clear stored multi-turn context for a user/session."""

    (conversation_store or _DEFAULT_CONVERSATION_STORE).clear(user_id, session_id)


def clear_user_conversation_history(
    user_id: str,
    *,
    conversation_store: InMemoryConversationStore | None = None,
) -> int:
    """Clear all process-local multi-turn context for one user."""

    return (conversation_store or _DEFAULT_CONVERSATION_STORE).clear_user(user_id)


def _prepare_conversation_request(
    request: UserRequest,
    *,
    conversation_store: InMemoryConversationStore,
    enable_conversation_history: bool,
) -> UserRequest:
    if not enable_conversation_history:
        return request
    metadata = dict(request.metadata)
    if metadata.get("reset_conversation") is True:
        conversation_store.clear(request.user_id, request.session_id)
    history = conversation_store.get(request.user_id, request.session_id)
    if not history:
        metadata.setdefault("conversation_history", [])
        metadata.setdefault("conversation_context_text", "")
        metadata.setdefault("conversation_turn_index", 1)
        return request.model_copy(update={"metadata": metadata}, deep=True)
    metadata["conversation_history"] = [turn.model_dump() for turn in history]
    metadata["conversation_context_text"] = _format_conversation_context(history)
    metadata["conversation_turn_index"] = len(history) + 1
    return request.model_copy(update={"metadata": metadata}, deep=True)


def _record_conversation_turn(
    state: AgentState,
    *,
    conversation_store: InMemoryConversationStore,
    enable_conversation_history: bool,
) -> None:
    if not enable_conversation_history or state.response is None or state.status == "failed":
        return
    user_text = (state.request.text or "").strip()
    assistant_text = state.response.message.strip()
    if not user_text or not assistant_text:
        return
    conversation_store.append(
        state.user_id,
        state.session_id,
        ConversationTurn(
            user_text=user_text,
            assistant_text=assistant_text,
            run_id=state.run_id,
            trace_id=state.trace_id,
        ),
    )


def _format_conversation_context(history: list[ConversationTurn]) -> str:
    lines: list[str] = []
    for index, turn in enumerate(history, start=1):
        lines.append(f"{index}. 用户：{turn.user_text}")
        lines.append(f"   助手：{turn.assistant_text}")
    return "\n".join(lines)


def _strip_env_value(value: str) -> str:
    quote_pairs = {('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")}
    comment_index = value.find(" #")
    if comment_index >= 0:
        value = value[:comment_index].strip()
    if len(value) >= 2 and (value[0], value[-1]) in quote_pairs:
        value = value[1:-1]
    return value.strip().strip('"').strip("'").strip("“”‘’")


def _safe_steps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _is_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("MULTIMODAL_AGENT_DISABLE_DOTENV") == "1"
