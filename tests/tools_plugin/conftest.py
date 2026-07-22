"""Explicit operator gate and governed fixtures for real tool calls."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.assistant_run_service import load_env_file
from assistant_agent.tools.registry import ToolRegistry, create_default_registry


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-real-tools-plugin",
        action="store_true",
        default=False,
        help="Run operator-requested tests that call real external tools.",
    )


@pytest.fixture(scope="session", autouse=True)
def require_explicit_operator_invocation(request: pytest.FixtureRequest) -> None:
    """Refuse real calls unless the operator supplied the dedicated CLI switch."""
    if not request.config.getoption("--run-real-tools-plugin"):
        pytest.fail(
            "tests/tools_plugin performs real external calls; rerun with "
            "--run-real-tools-plugin only after an explicit operator request."
        )


@pytest.fixture(scope="session")
def real_provider_config() -> ProviderConfig:
    load_env_file()
    config = ProviderConfig.from_env()
    if config.provider_mode != "real":
        pytest.fail("tests/tools_plugin requires MULTIMODAL_AGENT_PROVIDER_MODE=real")
    return config


@pytest.fixture(scope="session")
def real_tool_registry(real_provider_config: ProviderConfig) -> ToolRegistry:
    registry = create_default_registry(real_provider_config)
    if not registry.sealed:
        pytest.fail("real tool registry did not finish startup assembly")
    return registry


@pytest.fixture
def run_real_tool(real_tool_registry: ToolRegistry) -> Callable[[str, dict], ToolResult]:
    """Run one real tool through validation, execution, and registry governance."""

    def _run(tool_name: str, tool_input: dict) -> ToolResult:
        request = UserRequest(
            user_id="operator-real-tools-plugin",
            session_id=f"operator-real-tools-plugin-{tool_name}",
            text=f"Explicit operator smoke test for {tool_name}",
        )
        state = AgentState.from_request(
            request,
            run_id=f"operator-real-tools-plugin-{tool_name}",
        )
        validation = ActionValidator().validate(
            decision=AssistantDecision(
                type="tool_call",
                tool_name=tool_name,
                tool_input=tool_input,
            ),
            registry=real_tool_registry,
            request=request,
            state=state,
        )
        if not validation.accepted:
            pytest.fail(
                f"real tool validation failed for {tool_name}: "
                f"{validation.model_dump(mode='json')}"
            )
        return ToolExecutor(registry=real_tool_registry).run_tool(
            state,
            f"real-{tool_name}",
            tool_name,
            tool_input,
            validated_input=validation.validated_input,
        )

    return _run
