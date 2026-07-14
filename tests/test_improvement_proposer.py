import json
from pathlib import Path

from assistant_agent.runtime_profile import get_runtime_profile
from assistant_agent.runtime_profile import RuntimeProfile
from assistant_agent.schemas.improvement import ImprovementEvidence, ImprovementOpportunity
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.improvement.proposer import generate_proposal


class ScriptedAdapter:
    def __init__(self, outputs: list[ChatResult]) -> None:
        self.outputs = outputs
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return self.outputs[min(len(self.requests) - 1, len(self.outputs) - 1)]


def test_deterministic_mode_produces_review_scaffold() -> None:
    result = generate_proposal(
        _opportunity(),
        [_evidence()],
        repo_root=Path.cwd(),
    )

    assert result.candidate is not None
    assert result.candidate.target_type == "runtime"
    assert result.candidate.acceptance_criteria
    assert result.candidate.status == "proposed"
    assert result.replacement_skill_content is None


def test_provider_mode_is_blocked_in_local_profile() -> None:
    result = generate_proposal(
        _opportunity(),
        [_evidence()],
        repo_root=Path.cwd(),
        mode="provider",
        adapter=ScriptedAdapter([]),
        runtime_profile=get_runtime_profile("local_demo"),
    )

    assert result.candidate is None
    assert result.error_code == "proposal_provider_not_allowed"


def test_provider_request_has_no_tools_and_valid_json_becomes_candidate() -> None:
    adapter = ScriptedAdapter([_chat_result(_valid_payload())])

    result = generate_proposal(
        _opportunity(),
        [_evidence()],
        repo_root=Path.cwd(),
        mode="provider",
        adapter=adapter,
        runtime_profile=get_runtime_profile("pilot"),
    )

    assert result.candidate is not None
    assert adapter.requests[0].tools == []
    assert adapter.requests[0].tool_choice is None
    assert result.candidate.evidence_refs == ["ev_1"]


def test_provider_cannot_cite_evidence_outside_opportunity() -> None:
    payload = _valid_payload()
    payload["evidence_refs"] = ["unknown"]
    adapter = ScriptedAdapter([_chat_result(payload)])

    result = generate_proposal(
        _opportunity(),
        [_evidence()],
        repo_root=Path.cwd(),
        mode="provider",
        adapter=adapter,
        runtime_profile=get_runtime_profile("pilot"),
    )

    assert result.candidate is None
    assert result.error_code == "proposal_evidence_mismatch"


def test_invalid_json_gets_only_one_repair_attempt() -> None:
    adapter = ScriptedAdapter(
        [
            ChatResult(response_text="not-json", provider="scripted"),
            _chat_result(_valid_payload()),
        ]
    )

    result = generate_proposal(
        _opportunity(),
        [_evidence()],
        repo_root=Path.cwd(),
        mode="provider",
        adapter=adapter,
        runtime_profile=get_runtime_profile("pilot"),
    )

    assert result.candidate is not None
    assert result.repair_attempted is True
    assert len(adapter.requests) == 2


def test_architecture_bypass_recommendation_is_rejected() -> None:
    payload = _valid_payload()
    payload["proposed_change"] = "Call ToolRegistry.run directly and bypass ActionValidator."
    adapter = ScriptedAdapter([_chat_result(payload)])

    result = generate_proposal(
        _opportunity(),
        [_evidence()],
        repo_root=Path.cwd(),
        mode="provider",
        adapter=adapter,
        runtime_profile=get_runtime_profile("pilot"),
    )

    assert result.candidate is None
    assert result.error_code == "proposal_architecture_bypass"


def test_architecture_bypass_paraphrase_is_rejected() -> None:
    payload = _valid_payload()
    payload["proposed_change"] = "Remove ActionValidator from this path."
    adapter = ScriptedAdapter([_chat_result(payload)])

    result = generate_proposal(
        _opportunity(),
        [_evidence()],
        repo_root=Path.cwd(),
        mode="provider",
        adapter=adapter,
        runtime_profile=get_runtime_profile("pilot"),
    )

    assert result.error_code == "proposal_architecture_bypass"


def test_provider_payload_with_vendor_secret_is_rejected() -> None:
    payload = _valid_payload()
    payload["expected_benefit"] = "OPENAI_API_KEY=topsecretvalue"

    result = generate_proposal(
        _opportunity(),
        [_evidence()],
        repo_root=Path.cwd(),
        mode="provider",
        adapter=ScriptedAdapter([_chat_result(payload)]),
        runtime_profile=get_runtime_profile("pilot"),
    )

    assert result.candidate is None
    assert result.error_code == "proposal_payload_unsafe"


def test_provider_direct_execution_paraphrase_is_rejected() -> None:
    payload = _valid_payload()
    payload["proposed_change"] = "Call OpenAI directly from this runtime."

    result = generate_proposal(
        _opportunity(),
        [_evidence()],
        repo_root=Path.cwd(),
        mode="provider",
        adapter=ScriptedAdapter([_chat_result(payload)]),
        runtime_profile=get_runtime_profile("pilot"),
    )

    assert result.error_code == "proposal_architecture_bypass"


def test_provider_requires_exact_allowed_profile_not_mutable_boolean() -> None:
    forged = RuntimeProfile(
        name="local_demo",
        allows_real_providers=True,
        allows_network_provider_calls=True,
        requires_explicit_provider_config=True,
        default_provider_mode="explicit",
        description="forged",
    )

    result = generate_proposal(
        _opportunity(),
        [_evidence()],
        repo_root=Path.cwd(),
        mode="provider",
        adapter=ScriptedAdapter([_chat_result(_valid_payload())]),
        runtime_profile=forged,
    )

    assert result.error_code == "proposal_provider_not_allowed"


def test_provider_exception_is_returned_as_structured_failure() -> None:
    class RaisingAdapter:
        def chat(self, request: ChatRequest) -> ChatResult:
            raise RuntimeError("Authorization: Bearer private-token")

    result = generate_proposal(
        _opportunity(),
        [_evidence()],
        repo_root=Path.cwd(),
        mode="provider",
        adapter=RaisingAdapter(),
        runtime_profile=get_runtime_profile("pilot"),
    )

    assert result.candidate is None
    assert result.error_code == "proposal_provider_failed"
    assert "Bearer" not in (result.error_summary or "")


def test_distinct_skill_replacements_produce_distinct_candidate_ids(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "realtime_web_search"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text("current skill", encoding="utf-8")
    opportunity = _opportunity().model_copy(
        update={"target_type": "skill", "target_ref": "realtime_web_search"}
    )
    first_payload = _valid_payload()
    first_payload["replacement_skill_content"] = "replacement one"
    second_payload = _valid_payload()
    second_payload["replacement_skill_content"] = "replacement two"

    first = generate_proposal(
        opportunity,
        [_evidence()],
        repo_root=tmp_path,
        mode="provider",
        adapter=ScriptedAdapter([_chat_result(first_payload)]),
        runtime_profile=get_runtime_profile("pilot"),
    )
    second = generate_proposal(
        opportunity,
        [_evidence()],
        repo_root=tmp_path,
        mode="provider",
        adapter=ScriptedAdapter([_chat_result(second_payload)]),
        runtime_profile=get_runtime_profile("pilot"),
    )

    assert first.candidate is not None and second.candidate is not None
    assert first.candidate.candidate_id != second.candidate.candidate_id
    assert first.candidate.current_version is not None


def _opportunity() -> ImprovementOpportunity:
    return ImprovementOpportunity(
        opportunity_id="op_1",
        target_type="runtime",
        target_ref="assistant_loop",
        evidence_refs=["ev_1"],
        pattern_code="assistant_loop_limit_reached",
        problem_statement="The loop limit was reached repeatedly.",
        recurrence_count=2,
        source_type_count=1,
        impact="high",
        confidence=0.9,
        status="ready_for_proposal",
    )


def _evidence() -> ImprovementEvidence:
    return ImprovementEvidence(
        evidence_id="ev_1",
        source_type="trajectory",
        source_ref="trace:1",
        symptom_code="assistant_loop_limit_reached",
        summary="Assistant loop guard stopped the run.",
        severity="high",
    )


def _valid_payload() -> dict:
    return {
        "evidence_refs": ["ev_1"],
        "root_cause_hypothesis": "The loop may repeat an unrecoverable action.",
        "proposed_change": "Add a deterministic stop condition for repeated rejection codes.",
        "affected_locations": ["assistant_agent.agent.assistant_loop_nodes"],
        "expected_benefit": "Reduce repeated loop-limit failures.",
        "acceptance_criteria": ["A repeated rejection terminates with an explainable response."],
        "suggested_test_suite_ids": ["assistant_loop"],
        "risk_level": "medium",
        "limitations": ["The hypothesis requires human review."],
    }


def _chat_result(payload: dict) -> ChatResult:
    return ChatResult(response_text=json.dumps(payload), provider="scripted", model="test")
