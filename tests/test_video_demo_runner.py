from scripts.run_demo_flows import run_demo_flows


def test_video_demo_runner_has_video_scenarios() -> None:
    summary = run_demo_flows()
    ids = {result["scenario_id"] for result in summary["results"]}

    assert {"video_understanding", "video_to_product_search", "video_to_render"}.issubset(ids)


def test_video_demo_runner_runs_video_to_render_offline() -> None:
    summary = run_demo_flows("video_to_render")
    result = summary["results"][0]

    assert summary["total"] == 1
    assert summary["failed"] == 0
    assert result["tool_sequence"] == ["video_understanding", "render_3d"]
    assert result["checks"]["expected_tools_match"] is True
    assert result["checks"]["response_contains_match"] is True
    assert result["status"] == "succeeded"
