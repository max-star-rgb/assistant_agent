from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from assistant_agent.workflows.store import workflow_matches_claim_scope
from scripts import media_simulator


def test_media_progress_requires_strict_progress_projection() -> None:
    strict = {
        "progress": {
            "state": "working",
            "phase": "executing",
            "completed_items": 1,
            "total_items": 3,
            "active_items": [],
        },
        "plan": {"work_items": [{"result_summary": "legacy-secret"}]},
    }
    assert media_simulator.project_workflow_progress(strict) == strict["progress"]
    assert media_simulator.project_workflow_progress(
        {
            "workflow": {"status": "running"},
            "plan": {"work_items": [{"status": "running"}]},
        }
    ) == {}


@pytest.mark.parametrize(
    ("result_content", "expected", "forbidden"),
    [
        ("strict final report", "strict final report", "legacy summary"),
        ("", None, "legacy summary"),
    ],
)
def test_media_completion_reads_only_result_content(
    monkeypatch,
    capsys,
    result_content: str,
    expected: str | None,
    forbidden: str,
) -> None:
    def fake_get(_server, path, _user, _session, _query):
        if path.endswith("/events"):
            return {"events": [], "next_cursor": 1}
        if path.endswith("/result"):
            return {"content": result_content}
        return {
            "workflow": {"status": "completed", "phase": "completed"},
            "progress": {
                "state": "completed",
                "phase": "completed",
                "completed_items": 1,
                "total_items": 1,
                "active_items": [],
            },
            "plan": {
                "work_items": [
                    {"status": "succeeded", "result_summary": "legacy summary"}
                ]
            },
        }

    monkeypatch.setattr(media_simulator, "_workflow_api_get", fake_get)
    assert asyncio.run(
        media_simulator.tail_workflow(
            server="http://example.invalid",
            workflow_id="wf-product",
            user_number="user-product",
            session_id="session-product",
            poll_seconds=0,
        )
    )
    output = capsys.readouterr().out
    if expected is not None:
        assert expected in output
    assert forbidden not in output


def test_graph_v3_record_is_never_in_legacy_claim_scope() -> None:
    bundle = SimpleNamespace(
        workflow=SimpleNamespace(
            execution_engine="langgraph_v3", workflow_type="deep_research"
        )
    )
    assert not workflow_matches_claim_scope(
        bundle,
        allowed_execution_engines=frozenset(
            {"legacy_scheduler_v2", "langgraph_v3"}
        ),
        allowed_workflow_types=frozenset({"deep_research"}),
    )
