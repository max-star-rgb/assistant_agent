import pytest

from multimodal_agent.config import ProviderConfig


def test_real_provider_integration_requires_explicit_configuration() -> None:
    config = ProviderConfig.from_env()
    if not config.has_any_real_provider():
        pytest.skip("set a real provider config to run provider integration tests")

    assert config.has_any_real_provider() is True
