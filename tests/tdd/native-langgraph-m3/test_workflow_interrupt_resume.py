from __future__ import annotations

import asyncio
import json

import pytest

from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.workflows.durable_graph_app import (
    DurableWorkflowGraphApp,
    WorkflowGraphExecutionError,
    WorkflowGraphExecutionIdentity,
    WorkflowResume,
)
from assistant_agent.workflows.graph_state import latest_results

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

    def __init__(self, node_ids: tuple[str, ...]) -> None:
        self.node_ids = node_ids
        self.calls: dict[str, int] = {node_id: 0 for node_id in node_ids}

    def chat(self, request):
        node_id = next(node for node in self.node_ids if f"execute {node}" in request.user_query)
        self.calls[node_id] += 1
        response = (
            _blocked(node_id)
            if self.calls[node_id] == 1
            else json.dumps(
                {
                    "workflow_control": {
                        "outcome": "completed",
                        "summary": f"completed {node_id}",
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
