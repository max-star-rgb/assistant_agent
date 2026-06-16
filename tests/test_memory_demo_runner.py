from scripts.run_demo_flows import load_scenarios, run_demo_flows


def test_demo_runner_has_memory_scenarios() -> None:
    scenarios = load_scenarios()
    scenario_ids = {scenario["scenario_id"] for scenario in scenarios}

    assert {
        "memory_to_image_generation",
        "memory_product_to_render",
        "memory_task_resume",
        "memory_user_isolation",
    }.issubset(scenario_ids)


def test_demo_runner_runs_memory_product_to_render_offline() -> None:
    summary = run_demo_flows("memory_product_to_render")

    assert summary["failed"] == 0
    result = summary["results"][0]
    assert result["checks"]["expected_tools_match"] is True
    assert result["checks"]["response_contains_match"] is True
