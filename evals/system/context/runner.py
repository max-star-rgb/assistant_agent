"""Capture the compiled ChatRequest and exact Provider payload of one real turn."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import (
    ChatAdapter,
    ChatRequest,
    ChatResult,
    create_chat_adapter,
)
from assistant_agent.services.trace_store import JsonlTraceStore
from evals.system.common.artifacts import create_run_dir, write_json
from evals.system.common.preflight import validate_real_chat_config


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / ".data" / "evals" / "system" / "context"


class CapturingChatAdapter:
    """Transparent sync adapter that records both project and Provider requests."""

    def __init__(self, delegate: ChatAdapter) -> None:
        self.delegate = delegate
        self.provider = str(getattr(delegate, "provider", "unknown"))
        self.model = getattr(delegate, "model", None)
        self.compiled_requests: list[dict[str, Any]] = []
        self.provider_payloads: list[dict[str, Any]] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.compiled_requests.append(
            request.model_dump(mode="json", exclude={"provider_request_callback"})
        )

        def capture_provider_payload(payload: dict[str, Any]) -> None:
            self.provider_payloads.append(deepcopy(payload))

        forwarded = request.model_copy(
            update={"provider_request_callback": capture_provider_payload}
        )
        return self.delegate.chat(forwarded)


def run_context_system_eval(
    *,
    text: str,
    case_id: str = "single_turn_context",
    user_id: str = "system_context_eval_user",
    session_id: str = "system_context_eval_session",
    config: ProviderConfig | None = None,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> tuple[Path, dict[str, Any]]:
    """Execute one real turn and persist unredacted synthetic context evidence."""

    resolved = config or ProviderConfig.from_env()
    validate_real_chat_config(resolved)
    run_dir = create_run_dir(output_root, domain="context", case_id=case_id)
    trace_store = JsonlTraceStore(run_dir / "traces.jsonl")
    capture = CapturingChatAdapter(create_chat_adapter(resolved))
    runtime = AgentGraphRuntime(
        config=resolved,
        chat_adapter=capture,
        trace_store=trace_store,
    )
    try:
        state = runtime.run_state(
            UserRequest(
                user_id=user_id,
                session_id=session_id,
                text=text,
            )
        )
    finally:
        runtime.close()

    checks = {
        "completed": state.status == "completed",
        "compiled_request_captured": bool(capture.compiled_requests),
        "provider_payload_captured": bool(capture.provider_payloads),
    }
    result = {
        "schema_version": "system_context_eval_result_v1",
        "case_id": case_id,
        "passed": all(checks.values()),
        "checks": checks,
        "run_id": state.run_id,
        "trace_id": state.trace_id,
        "provider": capture.provider,
        "model": capture.model,
        "compiled_request_count": len(capture.compiled_requests),
        "provider_payload_count": len(capture.provider_payloads),
    }
    write_json(run_dir / "compiled_requests.json", capture.compiled_requests)
    write_json(run_dir / "provider_payloads.json", capture.provider_payloads)
    write_json(run_dir / "result.json", result)
    return run_dir, result
