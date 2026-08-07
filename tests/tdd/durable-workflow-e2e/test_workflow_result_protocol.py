from assistant_agent.workflows.agent_runtime import parse_work_item_response


def test_plain_work_item_text_remains_a_successful_artifact_result() -> None:
    result = parse_work_item_response(
        "plain result sentinel",
        run_id="run-sentinel",
        artifact_refs=[],
        model_calls_used=1,
        tool_calls_used=0,
    )

    assert result.status == "succeeded"
    assert result.summary == "plain result sentinel"


def test_valid_control_envelope_can_request_local_repair() -> None:
    result = parse_work_item_response(
        '{"workflow_control":{"status":"repair","summary":"evidence gap",'
        '"repair_work_item_ids":["collect_sources"]}}',
        run_id="run-sentinel",
        artifact_refs=[],
        model_calls_used=1,
        tool_calls_used=0,
    )

    assert result.status == "repair"
    assert result.summary == "evidence gap"
    assert result.repair_work_item_ids == ["collect_sources"]
