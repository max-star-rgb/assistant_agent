from __future__ import annotations

from assistant_agent.config import ProviderConfig
from assistant_agent.api.routes_agent import _public_request_metadata
from assistant_agent.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.gateway.session import _user_message_metadata
from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.tools.ids import WORKFLOW_SUBMIT_TOOL_NAME
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.workflows.definitions import (
    WorkflowDefinitionCatalog,
    WorkflowDefinitionDescriptor,
)
from assistant_agent.workflows.builtin import default_workflow_definitions
from assistant_agent.workflows.models import (
    WorkflowSubmission,
)
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.store import InMemoryWorkflowStore


class ToolProbeDefinition:
    descriptor = WorkflowDefinitionDescriptor(
        workflow_type="probe",
        definition_version="1",
    )

    def validate_submission(self, submission: WorkflowSubmission) -> None:
        return None

def _service() -> WorkflowService:
    return WorkflowService(
        store=InMemoryWorkflowStore(),
        definitions=WorkflowDefinitionCatalog([ToolProbeDefinition()]),
    )


def test_workflow_tool_is_registered_only_with_explicit_capability_binding() -> None:
    disabled = create_default_registry(ProviderConfig())
    enabled = create_default_registry(
        ProviderConfig(durable_workflows_enabled=True),
        workflow_service=_service(),
    )

    assert WORKFLOW_SUBMIT_TOOL_NAME not in disabled.list()
    assert WORKFLOW_SUBMIT_TOOL_NAME in enabled.list()
    assert "probe" in enabled.get_spec(WORKFLOW_SUBMIT_TOOL_NAME).description
    input_schema = enabled.get_spec(WORKFLOW_SUBMIT_TOOL_NAME).input_schema
    assert "initial_workstreams" not in input_schema["properties"]
    assert "planning_mode" not in input_schema["properties"]
    assert "constraint_bindings" not in input_schema["properties"]


def test_workflow_submit_runs_through_validator_and_executor() -> None:
    service = _service()
    registry = create_default_registry(
        ProviderConfig(durable_workflows_enabled=True),
        workflow_service=service,
    )
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="request-sentinel",
    )
    state = AgentState.from_request(
        request,
        run_id="run-sentinel",
        agent_id="agent-sentinel",
    )
    decision = AssistantToolCall(
        tool_name=WORKFLOW_SUBMIT_TOOL_NAME,
        tool_input={
            "workflow_type": "probe",
            "objective": "objective-sentinel",
            "deliverables": ["deliverable-sentinel"],
            "constraints": [],
            "inputs": {},
            "requested_budget": {},
            "durability_reasons": ["multi_stage"],
            "idempotency_key": "submission-sentinel",
        },
    )

    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )
    result = ToolExecutor(
        registry=registry,
        context_metadata={"workflow_service": service},
    ).run_tool(
        state,
        "workflow-submit-step",
        WORKFLOW_SUBMIT_TOOL_NAME,
        decision.tool_input,
        validated_input=validation.validated_input,
    )

    assert validation.accepted is True
    assert result.success is True
    assert result.data["workflow"]["status"] == "queued"
    workflow_id = result.data["workflow"]["workflow_id"]
    assert service.store.load(workflow_id) is not None
    assert state.tool_calls[0].tool_name == WORKFLOW_SUBMIT_TOOL_NAME


def test_standard_mode_cannot_silently_submit_a_deep_research_workflow() -> None:
    service = WorkflowService(
        store=InMemoryWorkflowStore(),
        definitions=default_workflow_definitions(),
    )
    registry = create_default_registry(
        ProviderConfig(durable_workflows_enabled=True),
        workflow_service=service,
    )
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="research-sentinel",
        assistant_mode="standard",
    )
    state = AgentState.from_request(
        request,
        run_id="run-sentinel",
        agent_id="agent-sentinel",
    )
    decision = AssistantToolCall(
        tool_name=WORKFLOW_SUBMIT_TOOL_NAME,
        tool_input={
            "workflow_type": "deep_research",
            "objective": "research-objective-sentinel",
            "deliverables": ["report-sentinel"],
            "inputs": {"research_questions": []},
            "durability_reasons": ["multi_stage"],
            "idempotency_key": "deep-research-submission-sentinel",
        },
    )
    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )

    result = ToolExecutor(
        registry=registry,
        context_metadata={"workflow_service": service},
    ).run_tool(
        state,
        "workflow-submit-step",
        WORKFLOW_SUBMIT_TOOL_NAME,
        decision.tool_input,
        validated_input=validation.validated_input,
    )

    assert result.success is False
    assert result.data == {"error": {"code": "assistant_mode_required"}}
    assert service.store.load_by_submission(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        ingress_run_id="run-sentinel",
        idempotency_key="deep-research-submission-sentinel",
    ) is None


def test_deep_research_mode_cannot_submit_a_non_research_workflow() -> None:
    service = _service()
    registry = create_default_registry(
        ProviderConfig(durable_workflows_enabled=True),
        workflow_service=service,
    )
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="research-sentinel",
        assistant_mode="deep_research",
    )
    state = AgentState.from_request(
        request,
        run_id="run-sentinel",
        agent_id="agent-sentinel",
    )
    decision = AssistantToolCall(
        tool_name=WORKFLOW_SUBMIT_TOOL_NAME,
        tool_input={
            "workflow_type": "probe",
            "objective": "research-objective-sentinel",
            "deliverables": ["report-sentinel"],
            "durability_reasons": ["multi_stage"],
            "idempotency_key": "wrong-mode-submission-sentinel",
        },
    )
    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )

    result = ToolExecutor(
        registry=registry,
        context_metadata={"workflow_service": service},
    ).run_tool(
        state,
        "workflow-submit-step",
        WORKFLOW_SUBMIT_TOOL_NAME,
        decision.tool_input,
        validated_input=validation.validated_input,
    )

    assert result.success is False
    assert result.data == {"error": {"code": "workflow_type_mode_mismatch"}}
    assert service.store.load_by_submission(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        ingress_run_id="run-sentinel",
        idempotency_key="wrong-mode-submission-sentinel",
    ) is None


def test_work_item_empty_allowlist_exposes_no_tools() -> None:
    registry = create_default_registry(ProviderConfig())
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="request-sentinel",
        metadata={
            "_trusted_workflow_assignment": {
                "workflow_id": "workflow-sentinel",
                "work_item_id": "step-sentinel",
                "attempt_id": "attempt-sentinel",
            },
            "_trusted_workflow_allowed_tools": [],
        },
    )

    selection = select_prompt_tool_specs(request, registry.list_specs())

    assert selection.run_tool_catalog.available_tool_names == []


def test_legacy_durable_task_quantum_cannot_recursively_submit_workflow() -> None:
    service = _service()
    registry = create_default_registry(
        ProviderConfig(durable_workflows_enabled=True),
        workflow_service=service,
    )
    request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="request-sentinel",
        task_execution_mode="durable",
    )
    decision = AssistantToolCall(
        tool_name=WORKFLOW_SUBMIT_TOOL_NAME,
        tool_input={
            "workflow_type": "probe",
            "objective": "objective-sentinel",
            "deliverables": ["deliverable-sentinel"],
            "durability_reasons": ["multi_stage"],
            "idempotency_key": "submission-sentinel",
        },
    )

    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=AgentState.from_request(request),
    )

    assert validation.accepted is False
    assert validation.code == "durable_plan_required"


def test_public_entries_strip_runtime_owned_workflow_metadata() -> None:
    forged = {
        "client_key": "client-value",
        "_trusted_workflow_assignment": {"workflow_id": "forged"},
        "_trusted_workflow_max_iterations": 999,
        "_trusted_workflow_allowed_tools": ["shell"],
    }

    http_metadata = _public_request_metadata(forged)
    websocket_metadata = _user_message_metadata({"metadata": forged})

    assert http_metadata == {"client_key": "client-value"}
    assert websocket_metadata == {"client_key": "client-value"}
