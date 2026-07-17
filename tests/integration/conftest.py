import os
from pathlib import Path

import pytest

from assistant_agent.config import should_run_integration_tests


REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PROVIDER_TOOL_ROOT = REPO_ROOT / "tests" / "integration" / "tools"
REAL_PROVIDER_TOOL_SELECTED = "ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED"
REAL_PROVIDER_TOOL_SELECTED_FILE_COUNT = (
    "ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED_FILE_COUNT"
)
REAL_PROVIDER_TOOL_SELECTED_FILE = "ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED_FILE"


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
    real_provider_tool_paths = _real_provider_tool_item_paths(items)
    real_provider_tool_only = real_provider_tool_paths is not None
    if real_provider_tool_only:
        os.environ[REAL_PROVIDER_TOOL_SELECTED] = "1"
        os.environ[REAL_PROVIDER_TOOL_SELECTED_FILE_COUNT] = str(
            len(real_provider_tool_paths)
        )
        if len(real_provider_tool_paths) == 1:
            os.environ[REAL_PROVIDER_TOOL_SELECTED_FILE] = str(
                real_provider_tool_paths[0]
            )
        else:
            os.environ.pop(REAL_PROVIDER_TOOL_SELECTED_FILE, None)
    else:
        os.environ.pop(REAL_PROVIDER_TOOL_SELECTED, None)
        os.environ.pop(REAL_PROVIDER_TOOL_SELECTED_FILE_COUNT, None)
        os.environ.pop(REAL_PROVIDER_TOOL_SELECTED_FILE, None)
    if should_run_integration_tests():
        return
    if real_provider_tool_only:
        return
    skip_integration = pytest.mark.skip(
        reason="set RUN_INTEGRATION_TESTS=1 to run integration tests"
    )
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(skip_integration)


def _real_provider_tool_item_paths(items) -> list[Path] | None:
    if not items:
        return None
    paths: set[Path] = set()
    for item in items:
        path = Path(str(item.path)).resolve()
        try:
            path.relative_to(REAL_PROVIDER_TOOL_ROOT)
        except ValueError:
            return None
        paths.add(path)
    return sorted(paths)
