from __future__ import annotations

from pydantic import BaseModel

from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.models import ToolResult
from evals.release_review.contracts import ReleaseScenario
from evals.release_review.decision_backend import ScenarioExecutionBackend
from evals.release_review.evidence import ReleaseRunEvidence
from tests.core.support import sealed_registry


def _scenario(*, outputs: list[dict] | None = None) -> ReleaseScenario:
    return ReleaseScenario.model_validate(
        {
            "id": "fixture_probe",
            "phase": "decision",
            "capability": "probe",
            "risk": "high",
            "request": "run probe",
            "tool_contract": {
                "required": ["probe_tool"],
                "allowed": [],
                "forbidden": [],
            },
            "fixtures": {
                "probe_tool": outputs
                or [
                    {"success": True, "data": {"ordinal": 1}},
                    {"success": False, "error": "dependency-sentinel"},
                ]
            },
        }
    )


def _context() -> ToolContext:
    return ToolContext(run_id="run-sentinel")


def test_backend_consumes_repeated_fixtures_and_records_calls() -> None:
    backend = ScenarioExecutionBackend(_scenario())
    registry = sealed_registry()

    first = backend.run(registry, "probe_tool", {"value": "first"}, _context())
    second = backend.run(registry, "probe_tool", {"value": "second"}, _context())

    assert first == ToolResult(
        tool_name="probe_tool", success=True, data={"ordinal": 1}
    )
    assert second.success is False
    assert second.error == "dependency-sentinel"
    assert [record.call_index for record in backend.calls] == [1, 2]
    assert [record.status for record in backend.calls] == ["succeeded", "failed"]
    assert backend.calls[1].input == {"value": "second"}


def test_backend_returns_structured_failure_for_missing_fixture() -> None:
    backend = ScenarioExecutionBackend(_scenario(outputs=[{"success": True, "data": {}}]))

    backend.run(sealed_registry(), "probe_tool", {"value": "first"}, _context())
    exhausted = backend.run(
        sealed_registry(), "probe_tool", {"value": "second"}, _context()
    )
    unknown = backend.run(
        sealed_registry(), "not_declared", {"value": "third"}, _context()
    )

    assert exhausted.success is False
    assert exhausted.error is not None
    assert exhausted.error.startswith("release_fixture_missing:")
    assert unknown.success is False
    assert unknown.error is not None
    assert unknown.error.startswith("release_fixture_missing:")


def test_backend_deep_copies_fixture_results_and_model_inputs() -> None:
    backend = ScenarioExecutionBackend(_scenario(outputs=[{"success": True, "data": {"nested": {"value": 1}}}]))

    class Input(BaseModel):
        value: str

    first = backend.run(sealed_registry(), "probe_tool", Input(value="sentinel"), _context())
    assert first.data is not None
    first.data["nested"]["value"] = 99

    assert backend.calls[0].input == {"value": "sentinel"}
    fixture_data = backend.scenario.fixtures["probe_tool"][0].data
    assert fixture_data == {"nested": {"value": 1}}


def test_evidence_projects_state_and_canonical_event_order() -> None:
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="request-sentinel",
    )
    state = AgentState.from_request(request)
    call = state.add_tool_call("probe_tool", {"value": "sentinel"})
    state.complete_tool_call(
        call.tool_call_id,
        ToolResult(tool_name="probe_tool", success=True, data={"value": "sentinel"}),
    )
    state.set_response(AgentResponse(message="done"))
    events = [
        {"canonical_event": "tool.started", "tool_name": "probe_tool"},
        {"canonical_event": "tool.finished", "tool_name": "probe_tool"},
        {"canonical_event": "response.delivered"},
    ]

    evidence = ReleaseRunEvidence.from_state(state, events)

    assert evidence.calls[0].call_index == 1
    assert evidence.calls[0].status == "succeeded"
    assert evidence.calls[0].before_final_response is True
    assert evidence.final_state["status"] == "completed"
    assert evidence.final_state["response"]["message"] == "done"
