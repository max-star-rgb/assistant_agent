from importlib import import_module

import pytest


def test_unused_process_local_notification_hub_is_not_packaged() -> None:
    with pytest.raises(ModuleNotFoundError) as caught:
        import_module("assistant_agent.api.agent_service_notifications")

    assert caught.value.name == "assistant_agent.api.agent_service_notifications"
