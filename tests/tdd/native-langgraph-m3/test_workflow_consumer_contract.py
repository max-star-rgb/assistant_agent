from __future__ import annotations

import asyncio
import pytest

from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore
from assistant_agent.workflows.store import InMemoryWorkflowStore, WorkflowStore
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
    assert (
        media_simulator.project_workflow_progress(
            {
                "workflow": {"status": "running"},
                "plan": {"work_items": [{"status": "running"}]},
            }
        )
        == {}
    )


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


def test_business_store_has_no_legacy_execution_authority() -> None:
    retired = {"claim_ready_work_item", "renew_work_item_lease"}
    for owner in (WorkflowStore, InMemoryWorkflowStore, SQLiteWorkflowStore):
        assert retired.isdisjoint(dir(owner))
