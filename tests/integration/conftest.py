import os
from pathlib import Path

import pytest

from assistant_agent.config import should_run_integration_tests


REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PROVIDER_TOOL_ROOT = REPO_ROOT / "tests" / "integration" / "tools"


def _load_dotenv_for_explicit_integration_runs() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, value = line.split("=", maxsplit=1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and not os.environ.get(key):
            os.environ[key] = value


_load_dotenv_for_explicit_integration_runs()


def pytest_collection_modifyitems(config, items):
    real_provider_tool_only = _all_items_under_real_provider_tool_root(items)
    if real_provider_tool_only:
        os.environ["ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED"] = "1"
    if should_run_integration_tests():
        return
    if real_provider_tool_only:
        return
    skip_integration = pytest.mark.skip(reason="set RUN_INTEGRATION_TESTS=1 to run integration tests")
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(skip_integration)


def _all_items_under_real_provider_tool_root(items) -> bool:
    if not items:
        return False
    for item in items:
        path = Path(str(item.path)).resolve()
        try:
            path.relative_to(REAL_PROVIDER_TOOL_ROOT)
        except ValueError:
            return False
    return True
