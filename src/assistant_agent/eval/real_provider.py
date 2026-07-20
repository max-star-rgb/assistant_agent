"""Opt-in real chat provider eval runner for end-to-end agent behavior."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentState
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.trace_store import JsonlTraceStore, TraceEvent


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CASES_PATH = PROJECT_ROOT / "evals" / "real_provider" / "personal_assistant_briefing.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / ".data" / "evals" / "real_provider"
REAL_PROVIDER_EVAL_PROFILES = {"provider_smoke", "pilot"}


class EvalConfigurationError(RuntimeError):
    """Raised when a real-provider eval is not explicitly configured."""


class RealProviderEvalCase(BaseModel):
    """One real chat provider eval case with trace-level expectations."""

    id: str = Field(min_length=1)
    suite: str = "personal_assistant_briefing"
    category: str = "personal_assistant"
    text: str = Field(min_length=1)
    user_id: str = "eval_user"
    session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    image_ids: list[str] = Field(default_factory=list)
    video_ids: list[str] = Field(default_factory=list)
    audio_id: str | None = None
    execution_strategy: Literal["react", "plan_and_solve"] = "react"
    task_execution_mode: Literal["auto", "durable", "foreground"] = "auto"
    expected_status: str = "completed"
    expected_tools: list[str] = Field(default_factory=list)
    expected_tool_sequence: list[str] = Field(default_factory=list)
    expected_exposed_tools: list[str] = Field(default_factory=list)
    must_not_call: list[str] = Field(default_factory=list)
    response_must_include: list[str] = Field(default_factory=list)
    response_must_include_any: list[list[str]] = Field(default_factory=list)
    min_tool_calls: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    notes: str | None = None


class RealProviderEvalCaseResult(BaseModel):
    """Machine-readable score for one real-provider eval case."""

    schema_version: Literal["real_provider_eval_case_result_v1"] = "real_provider_eval_case_result_v1"
    id: str
    suite: str
    category: str
    passed: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    run_id: str
    trace_id: str
    status: str
    response_present: bool
    response_preview: str
    expected_tools: list[str] = Field(default_factory=list)
    expected_tool_sequence: list[str] = Field(default_factory=list)
    actual_tools: list[str] = Field(default_factory=list)
    missing_expected_tools: list[str] = Field(default_factory=list)
    unexpected_tools: list[str] = Field(default_factory=list)
    expected_exposed_tools: list[str] = Field(default_factory=list)
    exposed_tools: list[str] = Field(default_factory=list)
    missing_exposed_tools: list[str] = Field(default_factory=list)
    excluded_reasons: dict[str, list[str]] = Field(default_factory=dict)
    response_missing_terms: list[str] = Field(default_factory=list)
    response_missing_keyword_groups: list[list[str]] = Field(default_factory=list)
    error_codes: list[str] = Field(default_factory=list)
    tool_count: int = 0
    trace_event_count: int = 0


class RealProviderEvalArtifact(BaseModel):
    """Paths written for one eval run."""

    run_dir: Path
    summary_path: Path
    results_path: Path
    trace_path: Path
    cases_path: Path


class RealProviderEvalRun(BaseModel):
    """Completed real-provider eval run."""

    artifact: RealProviderEvalArtifact
    summary: dict[str, Any]
    details: list[RealProviderEvalCaseResult]


def load_real_provider_eval_cases(path: Path | str = DEFAULT_CASES_PATH) -> list[RealProviderEvalCase]:
    """Load eval cases from a JSON array or an object with a ``cases`` array."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    defaults = payload.get("defaults", {}) if isinstance(payload, dict) else {}
    raw_cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(raw_cases, list):
        raise ValueError("real-provider eval cases must be a JSON array or an object with cases[]")
    return [
        RealProviderEvalCase.model_validate(_merge_case_defaults(defaults, item))
        for item in raw_cases
    ]


def filter_real_provider_eval_cases(
    cases: list[RealProviderEvalCase],
    *,
    suite: str | None = None,
    case_ids: set[str] | None = None,
    max_cases: int | None = None,
) -> list[RealProviderEvalCase]:
    """Return selected cases without mutating the loaded corpus."""

    selected = [
        case
        for case in cases
        if (suite in {None, "all"} or case.suite == suite)
        and (not case_ids or case.id in case_ids)
    ]
    if max_cases is not None:
        return selected[: max(0, max_cases)]
    return selected


def _merge_case_defaults(defaults: Any, case: Any) -> Any:
    if not isinstance(defaults, dict) or not isinstance(case, dict):
        return case
    merged = {**defaults, **case}
    default_metadata = defaults.get("metadata")
    case_metadata = case.get("metadata")
    if isinstance(default_metadata, dict) or isinstance(case_metadata, dict):
        merged["metadata"] = {
            **(default_metadata if isinstance(default_metadata, dict) else {}),
            **(case_metadata if isinstance(case_metadata, dict) else {}),
        }
    return merged


def validate_real_provider_config(config: ProviderConfig) -> None:
    """Fail unless the chat model is an explicit real provider under an opt-in profile."""

    profile_name = config.runtime_profile.name
    if profile_name not in REAL_PROVIDER_EVAL_PROFILES:
        raise EvalConfigurationError(
            "Real-provider eval requires MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke or pilot."
        )
    if config.chat_provider == "mock" or config.chat_adapter_kind == "mock":
        raise EvalConfigurationError(
            "Real-provider eval requires an explicit chat provider, "
            "for example MULTIMODAL_AGENT_CHAT_PROVIDER=deepseek."
        )
    missing = config.resolved_chat_provider().missing_required_env()
    if missing:
        joined = ", ".join(missing)
        raise EvalConfigurationError(f"Real-provider eval chat provider is missing: {joined}.")


def controlled_tool_provider_config(config: ProviderConfig, *, allow_real_tools: bool = False) -> ProviderConfig:
    """Return config that keeps chat real while defaulting non-chat tools to mock/local."""

    if allow_real_tools:
        return config
    return replace(
        config,
        vision_provider="mock",
        vision_api_key=None,
        vision_base_url=None,
        vision_model=None,
        vision_adapter_kind="mock",
        visual_image_search_provider="mock",
        qwen_image_search_api_key=None,
        image_generation_provider="mock",
        image_generation_api_key=None,
        image_generation_base_url=None,
        image_generation_model=None,
        image_generation_adapter_kind="mock",
        search_provider="mock",
        web_search_base_url=None,
        web_search_api_key=None,
        product_search_provider="mock",
        product_search_base_url=None,
        product_search_api_key=None,
        price_compare_provider="mock",
        price_compare_base_url=None,
        price_compare_api_key=None,
        haodanku_api_key=None,
        render_provider="mock",
        render_base_url=None,
        render_api_key=None,
        memory_backend="memory",
        memory_remote_service_adapter="unavailable",
        memory_plugin_enabled=False,
        conversation_history_backend="memory",
    )


def user_request_from_eval_case(case: RealProviderEvalCase) -> UserRequest:
    """Build the runtime request for one eval case."""

    return UserRequest(
        user_id=case.user_id,
        session_id=case.session_id or f"eval_{case.id}",
        text=case.text,
        image_ids=list(case.image_ids),
        video_ids=list(case.video_ids),
        audio_id=case.audio_id,
        execution_strategy=case.execution_strategy,
        task_execution_mode=case.task_execution_mode,
        metadata=dict(case.metadata),
    )


def evaluate_real_provider_state(
    case: RealProviderEvalCase,
    state: AgentState,
    *,
    trace_events: list[TraceEvent],
) -> RealProviderEvalCaseResult:
    """Score one completed runtime state against one eval case."""

    actual_tools = [call.tool_name for call in state.tool_calls]
    response_text = state.response.message if state.response else ""
    exposed_tools, excluded_reasons = _exposure_from_state_or_trace(state, trace_events)
    missing_expected_tools = [tool for tool in case.expected_tools if tool not in actual_tools]
    unexpected_tools = [tool for tool in actual_tools if tool in case.must_not_call]
    missing_exposed_tools = [tool for tool in case.expected_exposed_tools if tool not in exposed_tools]
    response_missing_terms = [term for term in case.response_must_include if term not in response_text]
    response_missing_keyword_groups = [
        group for group in case.response_must_include_any if not any(term in response_text for term in group)
    ]
    error_codes = _error_codes(state, trace_events)
    checks = {
        "status_match": state.status == case.expected_status,
        "response_present": bool(response_text.strip()),
        "expected_tools_match": not missing_expected_tools,
        "expected_tool_sequence_match": tools_contain_expected(actual_tools, case.expected_tool_sequence),
        "unexpected_tools_absent": not unexpected_tools,
        "expected_tools_exposed": not missing_exposed_tools,
        "response_terms_match": not response_missing_terms,
        "response_keyword_groups_match": not response_missing_keyword_groups,
        "min_tool_calls_match": case.min_tool_calls is None or len(actual_tools) >= case.min_tool_calls,
        "max_tool_calls_match": case.max_tool_calls is None or len(actual_tools) <= case.max_tool_calls,
        "no_runtime_errors": not error_codes,
    }
    failures = [name for name, passed in checks.items() if not passed]
    return RealProviderEvalCaseResult(
        id=case.id,
        suite=case.suite,
        category=case.category,
        passed=all(checks.values()),
        checks=checks,
        failures=failures,
        run_id=state.run_id,
        trace_id=state.trace_id,
        status=state.status,
        response_present=bool(response_text.strip()),
        response_preview=_preview(response_text),
        expected_tools=list(case.expected_tools),
        expected_tool_sequence=list(case.expected_tool_sequence),
        actual_tools=actual_tools,
        missing_expected_tools=missing_expected_tools,
        unexpected_tools=unexpected_tools,
        expected_exposed_tools=list(case.expected_exposed_tools),
        exposed_tools=exposed_tools,
        missing_exposed_tools=missing_exposed_tools,
        excluded_reasons=excluded_reasons,
        response_missing_terms=response_missing_terms,
        response_missing_keyword_groups=response_missing_keyword_groups,
        error_codes=error_codes,
        tool_count=len(actual_tools),
        trace_event_count=len(trace_events),
    )


def run_real_provider_eval_suite(
    cases: list[RealProviderEvalCase],
    *,
    config: ProviderConfig | None = None,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
    suite_name: str = "personal_assistant_briefing",
    allow_real_tools: bool = False,
) -> RealProviderEvalRun:
    """Run cases through AgentGraphRuntime with a real chat provider and write artifacts."""

    base_config = config or ProviderConfig.from_env()
    validate_real_provider_config(base_config)
    runtime_config = controlled_tool_provider_config(base_config, allow_real_tools=allow_real_tools)
    provider = runtime_config.chat_provider
    model = runtime_config.chat_model or runtime_config.resolved_chat_provider().model or "unknown"
    run_dir = _new_run_dir(Path(output_root), suite_name=suite_name, provider=provider, model=model)
    trace_store = JsonlTraceStore(run_dir / "traces.jsonl")
    details: list[RealProviderEvalCaseResult] = []
    for case in cases:
        runtime = AgentGraphRuntime(
            config=runtime_config,
            trace_store=trace_store,
        )
        state = runtime.run_state(user_request_from_eval_case(case))
        details.append(
            evaluate_real_provider_state(
                case,
                state,
                trace_events=trace_store.list_by_run(state.run_id),
            )
        )
    artifact = write_real_provider_eval_artifacts(
        output_root=run_dir,
        suite_name=suite_name,
        provider=provider,
        model=model,
        cases=cases,
        details=details,
        trace_events=[],
        output_root_is_run_dir=True,
    )
    return RealProviderEvalRun(
        artifact=artifact,
        summary=_summary(provider=provider, model=model, details=details),
        details=details,
    )


def write_real_provider_eval_artifacts(
    *,
    output_root: Path | str,
    suite_name: str,
    provider: str,
    model: str | None,
    cases: list[RealProviderEvalCase],
    details: list[RealProviderEvalCaseResult],
    trace_events: list[TraceEvent],
    output_root_is_run_dir: bool = False,
) -> RealProviderEvalArtifact:
    """Write machine-readable eval summary, per-case results, cases, and traces."""

    root = Path(output_root)
    run_dir = root if output_root_is_run_dir else _new_run_dir(root, suite_name=suite_name, provider=provider, model=model)
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.json"
    results_path = run_dir / "results.jsonl"
    trace_path = run_dir / "traces.jsonl"
    cases_path = run_dir / "cases.json"
    summary_path.write_text(
        json.dumps(_summary(provider=provider, model=model, details=details), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with results_path.open("w", encoding="utf-8") as file:
        for detail in details:
            file.write(json.dumps(detail.model_dump(mode="json"), ensure_ascii=False) + "\n")
    cases_path.write_text(
        json.dumps([case.model_dump(mode="json") for case in cases], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if trace_events:
        with trace_path.open("w", encoding="utf-8") as file:
            for event in trace_events:
                file.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
    else:
        trace_path.touch(exist_ok=True)
    return RealProviderEvalArtifact(
        run_dir=run_dir,
        summary_path=summary_path,
        results_path=results_path,
        trace_path=trace_path,
        cases_path=cases_path,
    )


def tools_contain_expected(actual_tools: list[str], expected_tools: list[str]) -> bool:
    """Return whether ``expected_tools`` appears as an ordered subsequence."""

    cursor = 0
    for tool_name in actual_tools:
        if cursor < len(expected_tools) and tool_name == expected_tools[cursor]:
            cursor += 1
    return cursor == len(expected_tools)


def _exposure_from_state_or_trace(
    state: AgentState,
    trace_events: list[TraceEvent],
) -> tuple[list[str], dict[str, list[str]]]:
    run_tool_set = _first_trace_run_tool_set(trace_events)
    if not run_tool_set and state.run_tool_set is not None:
        run_tool_set = state.run_tool_set.model_dump(mode="json")
    exposed = _string_list(run_tool_set.get("exposed_tool_names") if run_tool_set else [])
    raw_excluded = run_tool_set.get("excluded_reasons") if run_tool_set else {}
    excluded = {
        str(name): _string_list(reasons)
        for name, reasons in raw_excluded.items()
        if isinstance(name, str)
    } if isinstance(raw_excluded, dict) else {}
    return exposed, excluded


def _first_trace_run_tool_set(trace_events: list[TraceEvent]) -> dict[str, Any]:
    for event in trace_events:
        context = event.output_summary.get("context")
        if not isinstance(context, dict):
            continue
        run_tool_set = context.get("run_tool_set")
        if isinstance(run_tool_set, dict) and run_tool_set.get("registered_tool_names"):
            return run_tool_set
    return {}


def _error_codes(state: AgentState, trace_events: list[TraceEvent]) -> list[str]:
    codes: list[str] = []
    for error in state.errors:
        code = error.details.get("code")
        if isinstance(code, str) and code:
            codes.append(code)
    for event in trace_events:
        if event.error_code:
            codes.append(event.error_code)
        if isinstance(event.error, dict):
            code = event.error.get("code")
            if isinstance(code, str) and code:
                codes.append(code)
    return _dedupe(codes)


def _summary(
    *,
    provider: str,
    model: str | None,
    details: list[RealProviderEvalCaseResult],
) -> dict[str, Any]:
    total = len(details)
    passed = sum(1 for detail in details if detail.passed)
    failed_details = [detail for detail in details if not detail.passed]
    return {
        "schema_version": "real_provider_eval_summary_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": passed / total if total else 0.0,
        "failed_case_ids": [detail.id for detail in failed_details],
        "failure_counts": _failure_counts(failed_details),
    }


def _failure_counts(details: list[RealProviderEvalCaseResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for detail in details:
        for failure in detail.failures:
            counts[failure] = counts.get(failure, 0) + 1
    return counts


def _new_run_dir(root: Path, *, suite_name: str, provider: str, model: str | None) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = "_".join(
        item
        for item in (
            timestamp,
            _safe_name(suite_name),
            _safe_name(provider),
            _safe_name(model or "model"),
        )
        if item
    )
    return root / name


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value)[:80]


def _preview(text: str, max_chars: int = 500) -> str:
    text = text.strip()
    return text if len(text) <= max_chars else text[:max_chars] + "..."


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped
