"""开发期检查：服务启动使用最终组装完成的 Tool Registry。"""

import asyncio
from types import SimpleNamespace

from assistant_agent.api import app as api_app
from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.registry_factory import create_default_registry


def test_server_startup_reports_the_runtime_registry(monkeypatch) -> None:
    registry = create_default_registry(ProviderConfig())
    runtime = SimpleNamespace(
        registry=registry,
        durable_task_service=None,
        config=ProviderConfig(),
    )
    app = SimpleNamespace(state=SimpleNamespace())
    reported_registries = []
    monkeypatch.setattr(api_app.routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(
        api_app,
        "print_tool_registry_summary",
        reported_registries.append,
    )

    worker = asyncio.run(api_app.start_durable_task_worker(app))

    assert worker is None
    assert reported_registries == [registry]
    assert app.state.agent_runtime is runtime
