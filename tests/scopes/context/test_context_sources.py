from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentState
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.context import (
    AssistantContextPack,
    ContextSection,
    ContextSourceIssue,
    ContextSourceResult,
)
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.context.sources import (
    ContextSourceCoordinator,
    ContextSourceRequest,
)


def _section_payload() -> dict[str, object]:
    return {
        "section_id": "owner.soul",
        "kind": "soul",
        "title": "Owner persona",
        "content": "保持简洁。",
        "authority": "owner_persona",
        "stability": "semi_stable",
        "source_type": "editable_file",
        "source_ref": "editable_context:soul",
        "identity_scope": "local_owner",
        "max_chars": 2_000,
    }


def _request() -> UserRequest:
    return UserRequest(user_id="owner-1", session_id="session-1", text="你好")


def test_context_section_contract_is_strict_and_serializable() -> None:
    section = ContextSection.model_validate(_section_payload())

    restored = ContextSection.model_validate_json(section.model_dump_json())

    assert restored == section
    assert restored.schema_version == "context_section_v1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("section_id", ""),
        ("authority", "unknown"),
        ("stability", "mutable"),
        ("max_chars", -1),
    ],
)
def test_context_section_rejects_invalid_contract(field: str, value: object) -> None:
    payload = _section_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        ContextSection.model_validate(payload)


def test_context_source_result_allows_recoverable_empty_result() -> None:
    result = ContextSourceResult()

    assert result.sections == []
    assert result.issues == []
    assert result.used_last_known_good is False


def test_context_pack_defaults_to_no_editable_sections() -> None:
    pack = AssistantContextPack(request=_request())

    assert pack.context_sections == []


def test_agent_state_round_trip_preserves_context_source_result() -> None:
    state = AgentState.from_request(_request(), run_id="run_context_source")
    state.context_source_result = ContextSourceResult(
        sections=[ContextSection.model_validate(_section_payload())]
    )

    restored = AgentState.model_validate_json(state.model_dump_json())

    assert restored.context_source_result == state.context_source_result


class _RecordingSource:
    source_id = "soul"

    def __init__(self, result: ContextSourceResult | None = None) -> None:
        self.result = result or ContextSourceResult()
        self.call_count = 0

    def load(self, request: ContextSourceRequest) -> ContextSourceResult:
        self.call_count += 1
        return self.result


class _FailingSource:
    source_id = "soul"

    def load(self, request: ContextSourceRequest) -> ContextSourceResult:
        raise RuntimeError(f"cannot read {request.source_root}/SOUL.md with secret-value")


def _source_request(
    root: Path,
    *,
    enabled: bool = True,
    enabled_source_ids: set[str] | None = None,
) -> ContextSourceRequest:
    return ContextSourceRequest(
        user_id="owner-1",
        source_root=root,
        local_owner_user_id="owner-1",
        runtime_profile="local_demo",
        editable_context_enabled=enabled,
        section_char_budgets={"soul": 2_000},
        enabled_source_ids=enabled_source_ids if enabled_source_ids is not None else {"soul"},
    )


def _section(
    section_id: str = "owner.soul",
    *,
    sensitive: bool = False,
) -> ContextSection:
    payload = _section_payload()
    payload["section_id"] = section_id
    payload["sensitive"] = sensitive
    return ContextSection.model_validate(payload)


def test_coordinator_does_not_call_sources_when_disabled(tmp_path: Path) -> None:
    source = _RecordingSource()

    result = ContextSourceCoordinator([source]).load_once(
        _source_request(tmp_path, enabled=False)
    )

    assert result == ContextSourceResult()
    assert source.call_count == 0


def test_coordinator_calls_only_explicitly_enabled_source_ids(tmp_path: Path) -> None:
    source = _RecordingSource()

    result = ContextSourceCoordinator([source]).load_once(
        _source_request(tmp_path, enabled_source_ids=set())
    )

    assert result == ContextSourceResult()
    assert source.call_count == 0


def test_coordinator_rejects_duplicate_and_sensitive_sections(tmp_path: Path) -> None:
    source = _RecordingSource(
        ContextSourceResult(
            sections=[
                _section(),
                _section(),
                _section("owner.sensitive", sensitive=True),
            ]
        )
    )

    result = ContextSourceCoordinator([source]).load_once(_source_request(tmp_path))

    assert [section.section_id for section in result.sections] == ["owner.soul"]
    assert [issue.code for issue in result.issues] == [
        "context_source_duplicate_section_id",
        "context_source_sensitive_section_rejected",
    ]


def test_coordinator_rejects_whitespace_only_section_content(tmp_path: Path) -> None:
    payload = _section_payload()
    payload["content"] = "   \n\t"
    source = _RecordingSource(
        ContextSourceResult(sections=[ContextSection.model_validate(payload)])
    )

    result = ContextSourceCoordinator([source]).load_once(_source_request(tmp_path))

    assert result.sections == []
    assert [issue.code for issue in result.issues] == [
        "context_source_empty_section_rejected"
    ]


def test_coordinator_converts_source_exception_to_public_issue(tmp_path: Path) -> None:
    result = ContextSourceCoordinator([_FailingSource()]).load_once(
        _source_request(tmp_path)
    )

    assert result.sections == []
    assert [issue.code for issue in result.issues] == ["context_source_load_failed"]
    serialized = result.model_dump_json()
    assert str(tmp_path) not in serialized
    assert "secret-value" not in serialized


def test_coordinator_caps_issues(tmp_path: Path) -> None:
    issues = [
        ContextSourceIssue(
            code=f"issue_{index}",
            source_ref="editable_context:soul",
            public_message="Source issue.",
        )
        for index in range(20)
    ]
    source = _RecordingSource(ContextSourceResult(issues=issues))

    result = ContextSourceCoordinator([source]).load_once(_source_request(tmp_path))

    assert len(result.issues) == 16
    assert result.issues[-1].code == "issue_15"


class _RecordingCoordinator:
    def __init__(self, result: ContextSourceResult) -> None:
        self.result = result
        self.requests: list[ContextSourceRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def load_once(self, request: ContextSourceRequest) -> ContextSourceResult:
        self.requests.append(request)
        return self.result


class _FinalChatAdapter:
    provider = "scripted-native"

    def chat(self, request: ChatRequest) -> ChatResult:
        return ChatResult(
            response_text="完成。",
            finish_reason="stop",
            message_kind="final_answer",
            provider=self.provider,
            model="context-source-test",
        )


def test_runtime_loads_context_sources_once_per_run(tmp_path: Path) -> None:
    result = ContextSourceResult(sections=[_section()])
    coordinator = _RecordingCoordinator(result)
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            editable_context_enabled=True,
            editable_context_root=str(tmp_path),
            editable_context_user_id="owner-1",
        ),
        chat_adapter=_FinalChatAdapter(),
        context_source_coordinator=coordinator,
    )

    first = runtime.run_state(_request())
    second = runtime.run_state(
        UserRequest(user_id="owner-1", session_id="session-2", text="继续")
    )

    assert coordinator.call_count == 2
    assert coordinator.requests[0].user_id == "owner-1"
    assert coordinator.requests[0].source_root == tmp_path
    assert coordinator.requests[0].section_char_budgets == {"soul": 2_000}
    assert coordinator.requests[0].enabled_source_ids == {"soul"}
    assert first.context_source_result == result
    assert second.context_source_result == result
