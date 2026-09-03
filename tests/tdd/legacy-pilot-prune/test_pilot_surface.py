from importlib import import_module

import pytest


@pytest.mark.parametrize(
    ("module_name", "expected_missing"),
    [
        ("assistant_agent.api.identity", {"assistant_agent.api"}),
        ("assistant_agent.api.trial_access", {"assistant_agent.api"}),
        (
            "assistant_agent.multi_agent.agent_router",
            {"assistant_agent.multi_agent", "assistant_agent.multi_agent.agent_router"},
        ),
        (
            "assistant_agent.multi_agent.agent_pilot_readiness",
            {
                "assistant_agent.multi_agent",
                "assistant_agent.multi_agent.agent_pilot_readiness",
            },
        ),
    ],
)
def test_retired_pilot_module_is_not_packaged(
    module_name: str,
    expected_missing: set[str],
) -> None:
    with pytest.raises(ModuleNotFoundError) as caught:
        import_module(module_name)

    assert caught.value.name in expected_missing
