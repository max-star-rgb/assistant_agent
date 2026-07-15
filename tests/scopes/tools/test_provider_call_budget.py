from assistant_agent.services.provider_budget import ProviderCallBudget


def test_provider_call_budget_counts_calls_and_estimated_cost() -> None:
    budget = ProviderCallBudget(max_provider_calls_per_run=3)

    budget.record_call(
        run_id="run_1",
        capability="image_generation",
        provider="mock",
        model="mock-image",
        estimated_cost=0.01,
        cost_unit="usd",
        input_size_bytes=128,
        latency_ms=1,
        status="succeeded",
    )
    budget.record_call(
        run_id="run_1",
        capability="product_search",
        provider="local_json",
        estimated_cost=None,
        status="succeeded",
    )

    assert budget.provider_call_count == 2
    assert budget.capability_call_count("image_generation") == 1
    assert budget.estimated_cost_total == 0.01
    assert budget.summary()["calls_by_capability"] == {"image_generation": 1, "product_search": 1}


def test_provider_call_budget_blocks_max_calls_exceeded() -> None:
    budget = ProviderCallBudget(max_provider_calls_per_run=1)
    budget.record_call(run_id="run_1", capability="direct_chat", provider="mock", status="succeeded")

    error = budget.check_before_call(capability="image_generation", provider="mock")

    assert error is not None
    assert error.code == "provider_call_limit_exceeded"
    assert error.recoverable is False


def test_provider_call_budget_blocks_per_capability_limit() -> None:
    budget = ProviderCallBudget(max_provider_calls_per_run=5, max_calls_per_capability={"product_search": 1})
    budget.record_call(run_id="run_1", capability="product_search", provider="mock", status="succeeded")

    error = budget.check_before_call(capability="product_search", provider="mock")

    assert error is not None
    assert error.code == "provider_call_limit_exceeded"
    assert error.capability == "product_search"


def test_provider_call_budget_blocks_cost_and_input_size() -> None:
    cost_budget = ProviderCallBudget(max_estimated_cost_per_run=0.05)
    cost_error = cost_budget.check_before_call(capability="image_generation", estimated_cost=0.10)

    size_budget = ProviderCallBudget(max_input_bytes_per_run=8)
    size_error = size_budget.check_before_call(capability="video_understanding", input_size_bytes=16)

    assert cost_error is not None
    assert cost_error.code == "provider_budget_exceeded"
    assert size_error is not None
    assert size_error.code == "provider_input_size_exceeded"
