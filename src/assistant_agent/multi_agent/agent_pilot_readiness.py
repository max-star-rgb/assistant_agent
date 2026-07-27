"""Pilot-readiness checks and redacted replay summaries for agent control plane."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.provider_mode import ProviderMode, get_provider_mode
from assistant_agent.multi_agent.models import AgentArtifact, AgentTask, AgentTaskResult
from assistant_agent.multi_agent.agent_directory import AgentDirectory
from assistant_agent.api.identity import IdentityPolicy, IdentityPolicyDecision
from assistant_agent.providers.provider_errors import sanitize_error_detail, sanitize_error_message
from assistant_agent.providers.provider_readiness import ProviderReadinessReport


PilotCheckStatus = Literal["passed", "warning", "failed"]
PilotReadinessStatus = Literal["ready", "ready_with_warnings", "blocked"]


class PilotReadinessCheck(BaseModel):
    """One pilot readiness control check."""

    name: str = Field(min_length=1)
    status: PilotCheckStatus
    detail: dict[str, Any] = Field(default_factory=dict)


class PilotReadinessReport(BaseModel):
    """Aggregated pilot readiness report."""

    schema_version: str = "agent_pilot_readiness_v1"
    status: PilotReadinessStatus
    checks: list[PilotReadinessCheck] = Field(default_factory=list)


class PilotRunSummary(BaseModel):
    """Redacted control-plane summary for one delegated task result."""

    schema_version: str = "agent_pilot_run_summary_v1"
    task_id: str
    source_agent_id: str
    target_agent_id: str
    status: str
    run_id: str | None = None
    trace_id: str | None = None
    transport: str | None = None
    latency_ms: int | None = None
    budgets: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    remote: dict[str, Any] = Field(default_factory=dict)
    redaction: dict[str, Any] = Field(default_factory=dict)


class FailureReplayPayload(BaseModel):
    """Redacted payload that can be stored with a failed pilot run."""

    schema_version: str = "agent_failure_replay_v1"
    task: dict[str, Any]
    result: dict[str, Any]
    replay_notes: list[str] = Field(default_factory=list)


class PilotReadinessChecker:
    """Evaluate control-plane readiness without enabling remote or real providers."""

    def evaluate(
        self,
        *,
        directory: AgentDirectory | None = None,
        provider_mode: ProviderMode | None = None,
        allowlisted_hosts: list[str] | None = None,
        auth_bound_identity: bool = False,
        identity_policy: IdentityPolicyDecision | None = None,
        provider_readiness: ProviderReadinessReport | None = None,
    ) -> PilotReadinessReport:
        mode = provider_mode or get_provider_mode()
        checks = [
            self._provider_mode_check(mode),
            self._remote_opt_in_check(directory=directory, allowlisted_hosts=allowlisted_hosts or []),
            self._identity_check(
                identity_policy=identity_policy
                or IdentityPolicy().evaluate(
                    identity_source="auth_context" if auth_bound_identity else "request_body_or_local_context",
                    auth_bound_identity=auth_bound_identity,
                )
            ),
            PilotReadinessCheck(
                name="trace_redaction_default",
                status="passed",
                detail={"redaction_boundary": "sanitize_error_detail and TraceStore redaction"},
            ),
        ]
        if provider_readiness is not None:
            checks.insert(3, self._provider_readiness_check(provider_readiness))
        status: PilotReadinessStatus = "ready"
        if any(check.status == "failed" for check in checks):
            status = "blocked"
        elif any(check.status == "warning" for check in checks):
            status = "ready_with_warnings"
        return PilotReadinessReport(status=status, checks=checks)

    def _provider_mode_check(self, mode: ProviderMode) -> PilotReadinessCheck:
        if mode == "mock":
            return PilotReadinessCheck(
                name="mock_provider_mode",
                status="passed",
                detail={"provider_mode": mode},
            )
        if mode == "real":
            return PilotReadinessCheck(
                name="real_provider_mode",
                status="warning",
                detail={"provider_mode": mode},
            )
        return PilotReadinessCheck(
            name="provider_mode_invalid",
            status="failed",
            detail={"provider_mode": mode},
        )

    def _remote_opt_in_check(
        self,
        *,
        directory: AgentDirectory | None,
        allowlisted_hosts: list[str],
    ) -> PilotReadinessCheck:
        if directory is None:
            return PilotReadinessCheck(
                name="remote_a2a_default_disabled",
                status="passed",
                detail={"remote_agent_count": 0},
            )
        remote_instances = [
            instance
            for instance in directory.list(include_disabled=True)
            if "a2a_json_rpc" in set(instance.transports)
        ]
        if not remote_instances:
            return PilotReadinessCheck(
                name="remote_a2a_default_disabled",
                status="passed",
                detail={"remote_agent_count": 0},
            )
        missing_endpoint = [instance.agent_id for instance in remote_instances if not instance.endpoint_url]
        if missing_endpoint:
            return PilotReadinessCheck(
                name="remote_a2a_endpoint_configured",
                status="failed",
                detail={"missing_endpoint_agent_ids": missing_endpoint},
            )
        if not allowlisted_hosts:
            return PilotReadinessCheck(
                name="remote_a2a_allowlist_configured",
                status="failed",
                detail={"remote_agent_ids": [instance.agent_id for instance in remote_instances]},
            )
        return PilotReadinessCheck(
            name="remote_a2a_explicit_opt_in",
            status="passed",
            detail={
                "remote_agent_ids": [instance.agent_id for instance in remote_instances],
                "allowlisted_hosts": sorted(sanitize_error_message(host) for host in allowlisted_hosts),
            },
        )

    def _identity_check(self, *, identity_policy: IdentityPolicyDecision) -> PilotReadinessCheck:
        detail = identity_policy.model_dump(mode="json")
        detail.update(
            {
                "accepted_auth_sources": ["auth_context"],
                "auth_mode_env": "MULTIMODAL_AGENT_AUTH_MODE",
                "require_auth_bound_identity_env": "MULTIMODAL_AGENT_REQUIRE_AUTH_BOUND_IDENTITY",
                "header_auth_pilot_env": "MULTIMODAL_AGENT_AUTH_HEADER_ENABLED",
                "header_auth_default": "disabled",
                "mismatch_policy": "reject request body/path/query user_id when auth context user_id differs",
            }
        )
        return PilotReadinessCheck(
            name="auth_bound_identity",
            status=identity_policy.status,
            detail=detail,
        )

    def _provider_readiness_check(self, report: ProviderReadinessReport) -> PilotReadinessCheck:
        not_ready = [check for check in report.checks if check.status == "not_ready"]
        detail = {
            "provider_mode": report.provider_mode,
            "ready": report.ready,
            "checks": [
                {
                    "capability": check.capability,
                    "provider": check.provider,
                    "status": check.status,
                    "real_provider_allowed": check.real_provider_allowed,
                    "issue_codes": [issue.code for issue in check.issues],
                    "missing": [
                        missing
                        for issue in check.issues
                        for missing in issue.missing
                    ],
                }
                for check in report.checks
            ],
        }
        if not_ready:
            return PilotReadinessCheck(
                name="provider_config_explicit",
                status="failed",
                detail=detail,
            )
        return PilotReadinessCheck(
            name="provider_config_explicit",
            status="passed",
            detail=detail,
        )

def build_pilot_run_summary(
    *,
    task: AgentTask,
    result: AgentTaskResult,
) -> PilotRunSummary:
    """Build a redacted metrics summary for a delegated task result."""

    metadata = sanitize_error_detail(result.metadata)
    cost = _dict_or_empty(metadata.get("cost_summary"))
    return PilotRunSummary(
        task_id=task.task_id,
        source_agent_id=task.source_agent_id,
        target_agent_id=task.target_agent_id,
        status=result.status,
        run_id=result.run_id,
        trace_id=result.trace_id,
        transport=_string_or_none(metadata.get("transport")),
        latency_ms=_int_or_none(metadata.get("latency_ms")),
        budgets=_budget_summary(task=task, metadata=metadata),
        cost=sanitize_error_detail(cost),
        artifacts=_artifact_summary(result.artifacts),
        errors=[
            {
                "code": error.code,
                "message": sanitize_error_message(error.message),
                "recoverable": error.recoverable,
            }
            for error in result.errors
        ],
        remote=_remote_summary(metadata),
        redaction={
            "raw_payloads_included": False,
            "message_text": "preview_only",
            "metadata": "sanitized",
        },
    )


def build_failure_replay_payload(
    *,
    task: AgentTask,
    result: AgentTaskResult,
) -> FailureReplayPayload:
    """Build a redacted failure replay payload without raw provider/tool bodies."""

    summary = build_pilot_run_summary(task=task, result=result)
    task_metadata = _replay_metadata({**task.message.metadata, **task.metadata})
    return FailureReplayPayload(
        task={
            "task_id": task.task_id,
            "source_agent_id": task.source_agent_id,
            "target_agent_id": task.target_agent_id,
            "identity": {
                "user_id": sanitize_error_message(task.session.user_id),
                "session_id": sanitize_error_message(task.session.session_id),
                "correlation_id": sanitize_error_message(task.session.correlation_id),
            },
            "message": {
                "role": task.message.role,
                "text_preview": _preview(task.message.text),
                "image_count": len(task.message.image_ids),
                "video_count": len(task.message.video_ids),
                "has_audio": bool(task.message.audio_id),
            },
            "timeout_ms": task.timeout_ms,
            "delegation_depth": task.delegation_depth,
            "max_delegation_depth": task.max_delegation_depth,
            "token_budget": task.token_budget,
            "tool_budget": task.tool_budget,
            "metadata": task_metadata,
        },
        result=summary.model_dump(mode="json"),
        replay_notes=[
            "Replay payload is redacted and preview-only.",
            "Remote agents and real providers must be explicitly configured before replay.",
            "Do not treat body user_id/session_id as production auth identity.",
        ],
    )


def _budget_summary(*, task: AgentTask, metadata: dict[str, Any]) -> dict[str, Any]:
    child_budget = _dict_or_empty(metadata.get("child_context_budget"))
    delegation_budget = _dict_or_empty(metadata.get("delegation_budget"))
    return sanitize_error_detail(
        {
            "token_budget": task.token_budget,
            "tool_budget": task.tool_budget,
            "child_context_budget": child_budget,
            "delegation_budget": delegation_budget,
        }
    )


def _artifact_summary(artifacts: list[AgentArtifact]) -> dict[str, Any]:
    return {
        "artifact_count": len(artifacts),
        "kinds": sorted({artifact.kind for artifact in artifacts}),
        "output_ref_count": sum(len(artifact.output_refs) for artifact in artifacts),
        "text_chars": sum(len(artifact.text or "") for artifact in artifacts),
    }


def _remote_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "endpoint_host",
        "remote_task_id",
        "remote_context_id",
        "remote_status_state",
    )
    return sanitize_error_detail({key: metadata[key] for key in keys if key in metadata})


def _replay_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "agent_communication",
        "agent_context",
        "child_context_budget",
        "delegation_budget",
        "delegation_pairs",
        "endpoint_host",
        "latency_ms",
        "remote_status_state",
        "tool_result_refs",
        "transport",
    }
    return sanitize_error_detail({key: value for key, value in metadata.items() if key in allowed_keys})


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = sanitize_error_message(value)
    return text or None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _preview(value: str | None, *, max_chars: int = 120) -> str | None:
    if not value:
        return None
    text = sanitize_error_message(value)
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."
