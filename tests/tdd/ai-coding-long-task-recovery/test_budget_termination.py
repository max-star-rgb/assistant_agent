from langchain_core.messages import AIMessage, ToolMessage

from assistant_agent.coding.inspect_recovery import (
    extract_inspect_progress,
    render_inspect_recovery_context,
)


def test_extracts_tool_budget_from_structured_counter() -> None:
    result = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "coding_repo_read_file",
                    "args": {"path": "src/calc.py"},
                    "id": "call-1",
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content='{"path":"src/calc.py","content":"secret"}',
                tool_call_id="call-1",
                name="coding_repo_read_file",
            ),
        ],
        "run_tool_call_count": {"__all__": 13},
        "run_model_call_count": 2,
    }
    progress = extract_inspect_progress(
        result,
        epoch=1,
        base_commit="a" * 40,
        workspace_diff_digest="b" * 64,
        read_tool_names=frozenset({"coding_repo_read_file"}),
        model_call_limit=12,
        tool_call_limit=12,
    )
    assert progress is not None
    assert progress.reason == "tool_budget_exhausted"
    assert progress.calls[0].relative_paths == ("src/calc.py",)
    context = render_inspect_recovery_context((
        {"epoch": 1, "progress": progress, "outcome": "retrying"},
    ))
    assert "src/calc.py" in context
    assert "secret" not in context
    assert progress.progress_digest not in context


def test_extracts_model_budget_only_without_proposal() -> None:
    progress = extract_inspect_progress(
        {"messages": [], "run_tool_call_count": {"__all__": 0}, "run_model_call_count": 12},
        epoch=1,
        base_commit="a" * 40,
        workspace_diff_digest="b" * 64,
        read_tool_names=frozenset(),
        model_call_limit=12,
        tool_call_limit=12,
    )
    assert progress is not None
    assert progress.reason == "model_budget_exhausted"


def test_does_not_translate_unknown_or_successful_results() -> None:
    common = dict(
        epoch=1,
        base_commit="a" * 40,
        workspace_diff_digest="b" * 64,
        read_tool_names=frozenset(),
        model_call_limit=12,
        tool_call_limit=12,
    )
    assert extract_inspect_progress({"messages": []}, **common) is None
    assert extract_inspect_progress(
        {"messages": [ToolMessage(content="ok", tool_call_id="p", name="coding_propose_patch", artifact={"proposal": {}})], "run_model_call_count": 12},
        **common,
    ) is None

