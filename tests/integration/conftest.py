import pytest

from multimodal_agent.config import should_run_integration_tests


def pytest_collection_modifyitems(config, items):
    if should_run_integration_tests():
        return
    skip_integration = pytest.mark.skip(reason="set RUN_INTEGRATION_TESTS=1 to run integration tests")
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(skip_integration)
