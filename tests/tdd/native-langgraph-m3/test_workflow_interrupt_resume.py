from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.workflows.durable_graph_app import (
    DurableWorkflowGraphApp,
    WorkflowGraphExecutionError,
    WorkflowGraphExecutionIdentity,
    WorkflowResume,
    _pending_interrupts,
)
from assistant_agent.workflows.graph_state import (
    latest_results,
    validate_durable_workflow_state,
    WorkflowProfileAssignment,
)

from workflow_graph_probe import workflow_probe


def _blocked(node_id: str) -> str:
    return json.dumps(
        {
            "workflow_control": {
                "outcome": "blocked",
                "summary": f"need input for {node_id}",
                "required_fields": ["answer"],
                "prompt_code": "need_answer",
                "safe_prompt": f"Provide the answer for {node_id}.",
            }
        }
    )


class ResumeAwareWorker:
    provider = "scripted"
    model = "resume-worker"

    def __init__(
        self,
        node_ids: tuple[str, ...],
        *,
        initially_completed: frozenset[str] = frozenset(),
        blocked_attempts: int = 1,
    ) -> None:
        self.node_ids = node_ids
        self.initially_completed = initially_completed
        self.blocked_attempts = blocked_attempts
        self.calls: dict[str, int] = {node_id: 0 for node_id in node_ids}

    def chat(self, request):
        node_id = next(node for node in self.node_ids if f"execute {node}" in request.user_query)
        self.calls[node_id] += 1
        response = (
            _blocked(node_id)
            if self.calls[node_id] <= self.blocked_attempts
            and node_id not in self.initially_completed
            else json.dumps(
                {
                    "workflow_control": {
                        "outcome": "completed",
                        "summary": f"completed {node_id}",
                        "content": f"deliverable {node_id}",
                        "acceptance_evidence": [
                            {
                                "criterion_id": f"criterion_{node_id}",
                                "evidence": "delivered",
                            }
                        ],
                    }
                }
            )
        )
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text=response,
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def _identity(run_id: str) -> WorkflowGraphExecutionIdentity:
    return WorkflowGraphExecutionIdentity(
        workflow_id="wf-send",
        thread_id="workflow-thread-send",
        run_id=run_id,
        user_id="user-send",
        session_id="session-send",
        agent_id="agent-send",
    )


def test_parent_owns_parallel_interrupts_and_multi_resume_uses_new_generation(tmp_path):
    graph, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"a": [], "b": []}
    )
    worker = ResumeAwareWorker(("a", "b"))
    context.services.provider_registry["worker"] = worker
    app = DurableWorkflowGraphApp(graph)

    async def execute():
        interrupted = await app.arun(
            initial,
            identity=_identity("invoke-send"),
            context=context,
        )
        snapshot = await app.aget_state(_identity("invoke-send"))
        history = await app.aget_state_history(_identity("invoke-send"), limit=20)
        resumed = await app.aresume(
            identity=_identity("resume-send"),
            context=context,
            resume=WorkflowResume(
                values_by_action_ref={
                    "workflow:wf-send:node:a:generation:0": {"answer": "A"},
                    "workflow:wf-send:node:b:generation:0": {"answer": "B"},
                }
            ),
        )
        return interrupted, snapshot, history, resumed

    try:
        interrupted, snapshot, history, resumed = asyncio.run(execute())
        assert interrupted.status == "interrupted"
        assert {item.action_ref for item in interrupted.interrupts} == {
            "workflow:wf-send:node:a:generation:0",
            "workflow:wf-send:node:b:generation:0",
        }
        assert len(snapshot.tasks) == 2
        assert all(task.name == "await_branch_input" for task in snapshot.tasks)
        assert history
        assert any(item.tasks for item in history)
        assert {part.type for part in interrupted.parts}.issuperset(
            {"updates", "tasks", "checkpoints"}
        )
        assert resumed.status == "completed"
        assert resumed.final_state["invocation_run_id"] == "resume-send"
        assert resumed.final_state["publish_commit_ref"]["status"] == "committed"
        assert (
            context.services.publish_store.completed_event_count(
                resumed.final_state["publish_commit_ref"]["operation_key"]
            )
            == 1
        )
        assert (
            context.services.publisher.effect_count(
                resumed.final_state["publish_commit_ref"]["operation_key"]
            )
            == 1
        )
        assert resumed.final_state["execution_generation_by_node"] == {"a": 1, "b": 1}
        current = latest_results(
            resumed.final_state["result_ledger"],
            resumed.final_state["execution_generation_by_node"],
        )
        assert {node_id: result.status for node_id, result in current.items()} == {
            "a": "succeeded",
            "b": "succeeded",
        }
        assert worker.calls == {"a": 2, "b": 2}
    finally:
        artifact_store.close()


def test_repeated_resume_replaces_prior_resume_constraint(tmp_path):
    graph, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"a": []}
    )
    context.services.provider_registry["worker"] = ResumeAwareWorker(
        ("a",), blocked_attempts=2
    )
    app = DurableWorkflowGraphApp(graph)

    async def execute():
        first = await app.arun(
            initial,
            identity=_identity("invoke-send"),
            context=context,
        )
        second = await app.aresume(
            identity=_identity("resume-send-1"),
            context=context,
            resume=WorkflowResume(
                values_by_action_ref={
                    first.interrupts[0].action_ref: {"answer": "A"}
                }
            ),
        )
        return await app.aresume(
            identity=_identity("resume-send-2"),
            context=context,
            resume=WorkflowResume(
                values_by_action_ref={
                    second.interrupts[0].action_ref: {"answer": "A"}
                }
            ),
        )

    try:
        result = asyncio.run(execute())
        assert result.status == "completed"
        assert len(result.final_state["consumed_action_refs"]) == 2
    finally:
        artifact_store.close()


def test_partial_multi_resume_keeps_unprovided_parent_interrupt_pending(tmp_path):
    graph, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"a": [], "b": []}
    )
    worker = ResumeAwareWorker(("a", "b"))
    context.services.provider_registry["worker"] = worker
    app = DurableWorkflowGraphApp(graph)

    async def execute():
        await app.arun(initial, identity=_identity("invoke-send"), context=context)
        partial = await app.aresume(
            identity=_identity("resume-one"),
            context=context,
            resume=WorkflowResume(
                values_by_action_ref={
                    "workflow:wf-send:node:a:generation:0": {"answer": "A"}
                }
            ),
        )
        final = await app.aresume(
            identity=_identity("resume-two"),
            context=context,
            resume=WorkflowResume(
                values_by_action_ref={
                    "workflow:wf-send:node:b:generation:0": {"answer": "B"}
                }
            ),
        )
        return partial, final

    try:
        partial, final = asyncio.run(execute())
        assert partial.status == "interrupted"
        assert [item.action_ref for item in partial.interrupts] == [
            "workflow:wf-send:node:b:generation:0"
        ]
        assert final.status == "completed"
        assert worker.calls == {"a": 2, "b": 2}
    finally:
        artifact_store.close()


def test_resume_fails_closed_for_unknown_action_or_reused_run(tmp_path):
    graph, context, initial, _worker, artifact_store = workflow_probe(tmp_path, {"a": []})
    worker = ResumeAwareWorker(("a",))
    context.services.provider_registry["worker"] = worker
    app = DurableWorkflowGraphApp(graph)

    async def execute():
        await app.arun(initial, identity=_identity("invoke-send"), context=context)
        with pytest.raises(WorkflowGraphExecutionError) as unknown:
            await app.aresume(
                identity=_identity("resume-send"),
                context=context,
                resume=WorkflowResume(
                    values_by_action_ref={"workflow:wf-send:node:x:generation:0": {"answer": "X"}}
                ),
            )
        with pytest.raises(WorkflowGraphExecutionError) as reused:
            await app.aresume(
                identity=_identity("invoke-send"),
                context=context,
                resume=WorkflowResume(
                    values_by_action_ref={"workflow:wf-send:node:a:generation:0": {"answer": "A"}}
                ),
            )
        return unknown.value.code, reused.value.code

    try:
        assert asyncio.run(execute()) == (
            "workflow_resume_action_unknown",
            "workflow_resume_run_id_reused",
        )
        assert worker.calls == {"a": 1}
    finally:
        artifact_store.close()


def test_partial_resume_run_id_cannot_be_reused(tmp_path):
    graph, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"a": [], "b": []}
    )
    context.services.provider_registry["worker"] = ResumeAwareWorker(("a", "b"))
    app = DurableWorkflowGraphApp(graph)

    async def execute():
        await app.arun(initial, identity=_identity("invoke-send"), context=context)
        await app.aresume(
            identity=_identity("resume-once"),
            context=context,
            resume=WorkflowResume(
                values_by_action_ref={
                    "workflow:wf-send:node:a:generation:0": {"answer": "A"}
                }
            ),
        )
        with pytest.raises(WorkflowGraphExecutionError) as reused:
            await app.aresume(
                identity=_identity("resume-once"),
                context=context,
                resume=WorkflowResume(
                    values_by_action_ref={
                        "workflow:wf-send:node:b:generation:0": {"answer": "B"}
                    }
                ),
            )
        return reused.value.code

    try:
        assert asyncio.run(execute()) == "workflow_resume_run_id_reused"
    finally:
        artifact_store.close()


def test_resume_payload_is_bounded_before_graph_command(tmp_path):
    graph, context, initial, _worker, artifact_store = workflow_probe(tmp_path, {"a": []})
    context.services.provider_registry["worker"] = ResumeAwareWorker(("a",))
    app = DurableWorkflowGraphApp(graph)

    async def execute():
        await app.arun(initial, identity=_identity("invoke-send"), context=context)
        with pytest.raises(ValueError):
            WorkflowResume(
                values_by_action_ref={
                    "workflow:wf-send:node:a:generation:0": {"answer": "x" * 4001}
                }
            )
        snapshot = await app.aget_state(_identity("invoke-send"))
        return tuple(task.name for task in snapshot.tasks)

    try:
        assert asyncio.run(execute()) == ("await_branch_input",)
    finally:
        artifact_store.close()


def test_mixed_success_and_blocked_wave_resumes_only_blocked_assignment(tmp_path):
    graph, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"a": [], "b": []}
    )
    worker = ResumeAwareWorker(
        ("a", "b"), initially_completed=frozenset({"a"})
    )
    context.services.provider_registry["worker"] = worker
    app = DurableWorkflowGraphApp(graph)

    async def execute():
        interrupted = await app.arun(
            initial, identity=_identity("invoke-send"), context=context
        )
        resumed = await app.aresume(
            identity=_identity("resume-mixed"),
            context=context,
            resume=WorkflowResume(
                values_by_action_ref={
                    "workflow:wf-send:node:b:generation:0": {"answer": "B"}
                }
            ),
        )
        return interrupted, resumed

    try:
        interrupted, resumed = asyncio.run(execute())
        assert [item.action_ref for item in interrupted.interrupts] == [
            "workflow:wf-send:node:b:generation:0"
        ]
        assert resumed.status == "completed"
        assert worker.calls == {"a": 1, "b": 2}
        assert resumed.final_state["execution_generation_by_node"] == {"a": 0, "b": 1}
    finally:
        artifact_store.close()


def test_snapshot_mapping_rejects_wrong_task_name_and_generation(tmp_path):
    graph, context, initial, _worker, artifact_store = workflow_probe(tmp_path, {"a": []})
    context.services.provider_registry["worker"] = ResumeAwareWorker(("a",))
    app = DurableWorkflowGraphApp(graph)

    async def execute():
        await app.arun(initial, identity=_identity("invoke-send"), context=context)
        raw = await app._aget_raw_state(_identity("invoke-send"))
        state = validate_durable_workflow_state(raw.values)
        native = raw.tasks[0].interrupts[0]
        wrong_name = SimpleNamespace(
            tasks=(
                SimpleNamespace(
                    name="run_worker",
                    result=None,
                    interrupts=(native,),
                ),
            )
        )
        with pytest.raises(WorkflowGraphExecutionError) as name_error:
            _pending_interrupts(wrong_name, state)
        payload = dict(native.value)
        payload["execution_generation"] = 1
        payload["action_ref"] = "workflow:wf-send:node:a:generation:1"
        wrong_generation = SimpleNamespace(
            tasks=(
                SimpleNamespace(
                    name="await_branch_input",
                    result=None,
                    interrupts=(SimpleNamespace(id=native.id, value=payload),),
                ),
            )
        )
        with pytest.raises(WorkflowGraphExecutionError) as generation_error:
            _pending_interrupts(wrong_generation, state)
        assignment = WorkflowProfileAssignment.model_validate_json(
            json.dumps(state["active_wave"][0])
        )
        changed = assignment.model_dump(mode="python", exclude={"assignment_ref"})
        changed["user_id"] = "other-user"
        foreign = WorkflowProfileAssignment.create(**changed)
        foreign_state = dict(state)
        foreign_state["active_wave"] = [foreign.model_dump(mode="json")]
        foreign_payload = dict(native.value)
        foreign_payload["assignment_ref"] = foreign.assignment_ref
        foreign_snapshot = SimpleNamespace(
            tasks=(
                SimpleNamespace(
                    name="await_branch_input",
                    result=None,
                    interrupts=(
                        SimpleNamespace(id=native.id, value=foreign_payload),
                    ),
                ),
            )
        )
        with pytest.raises(WorkflowGraphExecutionError) as owner_error:
            _pending_interrupts(foreign_snapshot, foreign_state)
        return name_error.value.code, generation_error.value.code, owner_error.value.code

    try:
        assert asyncio.run(execute()) == (
            "workflow_interrupt_task_invalid",
            "workflow_interrupt_mapping_invalid",
            "workflow_interrupt_mapping_invalid",
        )
    finally:
        artifact_store.close()


def test_state_and_history_reject_cross_owner_identity(tmp_path):
    graph, context, initial, _worker, artifact_store = workflow_probe(tmp_path, {"a": []})
    context.services.provider_registry["worker"] = ResumeAwareWorker(("a",))
    app = DurableWorkflowGraphApp(graph)
    intruder = WorkflowGraphExecutionIdentity(
        workflow_id="wf-send",
        thread_id="workflow-thread-send",
        run_id="intruder-read",
        user_id="other-user",
        session_id="session-send",
        agent_id="agent-send",
    )

    async def execute():
        await app.arun(initial, identity=_identity("invoke-send"), context=context)
        with pytest.raises(WorkflowGraphExecutionError) as state_error:
            await app.aget_state(intruder)
        with pytest.raises(WorkflowGraphExecutionError) as history_error:
            await app.aget_state_history(intruder, limit=10)
        safe = await app.aget_state(_identity("invoke-send"))
        return state_error.value.code, history_error.value.code, safe

    try:
        state_code, history_code, safe = asyncio.run(execute())
        assert state_code == history_code == "workflow_resume_identity_mismatch"
        assert not hasattr(safe, "config")
        assert all(not hasattr(interrupt, "id") for task in safe.tasks for interrupt in task.interrupts)
    finally:
        artifact_store.close()
