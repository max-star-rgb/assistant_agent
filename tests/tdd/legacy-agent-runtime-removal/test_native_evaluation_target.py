from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage

from assistant_agent.evaluation.native_graph_target import NativeGraphEvaluationTarget


def test_native_evaluation_target_runs_the_production_parent_graph(monkeypatch) -> None:
    """Catches evaluation accidentally falling back to the retired Runtime graph."""

    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")

    async def exercise():
        target = await NativeGraphEvaluationTarget.open()
        try:
            return await target.ainvoke(
                user_id="evaluation-user",
                tenant_id="evaluation-tenant",
                thread_id="evaluation-thread",
                run_id="evaluation-run",
                text="request-sentinel",
                execution_mode="fast",
            )
        finally:
            await target.aclose()

    result = asyncio.run(exercise())

    assert result.thread_id == "evaluation-thread"
    assert result.run_id == "evaluation-run"
    assert isinstance(result.messages[-1], AIMessage)
    assert result.response_text == "已收到：request-sentinel"
