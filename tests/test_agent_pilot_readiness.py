from multimodal_agent.runtime_profile import get_runtime_profile
from multimodal_agent.schemas.agent_communication import (
    DEFAULT_AGENT_ID,
    AgentArtifact,
    AgentCommunicationError,
    AgentInstance,
    AgentMessage,
    AgentSessionRef,
    AgentTask,
    AgentTaskResult,
)
from multimodal_agent.services.agent_directory import AgentDirectory
from multimodal_agent.services.agent_pilot_readiness import (
    PilotReadinessChecker,
    build_failure_replay_payload,
    build_pilot_run_summary,
)
from multimodal_agent.services.api_identity import IdentityPolicy, resolve_request_identity


def test_pilot_readiness_checker_reports_safe_default_profile() -> None:
    report = PilotReadinessChecker().evaluate(runtime_profile=get_runtime_profile("local_demo"))

    assert report.status == "ready_with_warnings"
    checks = {check.name: check for check in report.checks}
    assert checks["default_profile_mock_local_offline"].status == "passed"
    assert checks["remote_a2a_default_disabled"].status == "passed"
    assert checks["auth_bound_identity"].status == "warning"
    assert checks["auth_bound_identity"].detail["header_auth_pilot_env"] == "MULTIMODAL_AGENT_AUTH_HEADER_ENABLED"
    assert checks["auth_bound_identity"].detail["header_auth_default"] == "disabled"
    assert checks["auth_bound_identity"].detail["mismatch_policy"].startswith("reject")
    assert checks["trace_redaction_default"].status == "passed"


def test_pilot_readiness_checker_blocks_remote_agent_without_allowlist() -> None:
    directory = AgentDirectory(
        [
            AgentInstance(
                agent_id="agent.remote",
                display_name="Remote Agent",
                transports=["a2a_json_rpc"],
                endpoint_url="https://remote.example/a2a/rpc",
            )
        ]
    )

    report = PilotReadinessChecker().evaluate(
        directory=directory,
        runtime_profile=get_runtime_profile("local_demo"),
    )

    assert report.status == "blocked"
    checks = {check.name: check for check in report.checks}
    assert checks["remote_a2a_allowlist_configured"].status == "failed"


def test_pilot_readiness_checker_accepts_explicit_remote_allowlist_and_auth_identity() -> None:
    directory = AgentDirectory(
        [
            AgentInstance(
                agent_id="agent.remote",
                display_name="Remote Agent",
                transports=["a2a_json_rpc"],
                endpoint_url="https://remote.example/a2a/rpc",
            )
        ]
    )

    report = PilotReadinessChecker().evaluate(
        directory=directory,
        runtime_profile=get_runtime_profile("local_demo"),
        allowlisted_hosts=["remote.example"],
        auth_bound_identity=True,
    )

    assert report.status == "ready"
    checks = {check.name: check for check in report.checks}
    assert checks["remote_a2a_explicit_opt_in"].status == "passed"
    assert checks["auth_bound_identity"].status == "passed"


def test_pilot_readiness_checker_blocks_production_required_request_identity() -> None:
    identity = resolve_request_identity(user_id="u1", session_id="s1", source="request_body")
    identity_policy = IdentityPolicy().evaluate(identity, production_required=True)

    report = PilotReadinessChecker().evaluate(
        runtime_profile=get_runtime_profile("local_demo"),
        identity_policy=identity_policy,
    )

    assert report.status == "blocked"
    checks = {check.name: check for check in report.checks}
    assert checks["auth_bound_identity"].status == "failed"
    assert checks["auth_bound_identity"].detail["production_required"] is True


def test_pilot_run_summary_redacts_sensitive_metadata_and_reports_metrics() -> None:
    task = _task()
    result = AgentTaskResult(
        task_id=task.task_id,
        target_agent_id="agent.remote",
        status="failed",
        run_id="remote_run_1",
        trace_id="remote_trace_1",
        artifacts=[AgentArtifact(kind="text", text="failed output", output_refs=["local://safe-ref"])],
        errors=[
            AgentCommunicationError(
                code="agent_remote_timeout",
                message="Remote timeout bearer sk-secret",
                detail={"api_key": "sk-secret", "raw_provider_response": "raw body"},
                recoverable=True,
            )
        ],
        metadata={
            "transport": "a2a_json_rpc",
            "latency_ms": 42,
            "endpoint_host": "remote.example",
            "remote_task_id": "remote_task_1",
            "remote_status_state": "failed",
            "authorization": "Bearer sk-secret",
            "raw_provider_response": "raw provider payload",
            "child_context_budget": {"token_budget": 100, "tool_budget": 2},
        },
    )

    summary = build_pilot_run_summary(task=task, result=result)
    serialized = summary.model_dump_json()

    assert summary.transport == "a2a_json_rpc"
    assert summary.latency_ms == 42
    assert summary.budgets["child_context_budget"] == {"token_budget": 100, "tool_budget": 2}
    assert summary.artifacts["artifact_count"] == 1
    assert summary.errors[0]["code"] == "agent_remote_timeout"
    assert summary.remote["endpoint_host"] == "remote.example"
    assert "sk-secret" not in serialized
    assert "raw provider payload" not in serialized
    assert "raw body" not in serialized


def test_failure_replay_payload_is_preview_only_and_omits_raw_context() -> None:
    task = _task(text="this is a long replay-safe user request " * 12)
    result = AgentTaskResult(
        task_id=task.task_id,
        target_agent_id="agent.remote",
        status="failed",
        errors=[AgentCommunicationError(code="agent_remote_protocol_error", message="bad json")],
        metadata={
            "transport": "a2a_json_rpc",
            "latency_ms": 5,
            "agent_context": {"omitted_context_count": 2},
            "raw_provider_response": "raw provider body",
        },
    )

    replay = build_failure_replay_payload(task=task, result=result)
    payload = replay.model_dump(mode="json")
    serialized = replay.model_dump_json()

    assert payload["task"]["message"]["text_preview"].endswith("...")
    assert len(payload["task"]["message"]["text_preview"]) <= 120
    assert payload["task"]["metadata"]["agent_context"] == {"omitted_context_count": 1}
    assert "raw provider body" not in serialized
    assert "parent should not replay" not in serialized


def _task(*, text: str = "delegate remote work") -> AgentTask:
    return AgentTask(
        source_agent_id=DEFAULT_AGENT_ID,
        target_agent_id="agent.remote",
        session=AgentSessionRef(user_id="u1", session_id="s1", correlation_id="corr_pilot"),
        message=AgentMessage(
            text=text,
            metadata={
                "agent_context": {"omitted_context_count": 1},
                "parent_history": "parent should not replay",
                "api_key": "sk-secret",
            },
        ),
        token_budget=100,
        tool_budget=2,
    )
