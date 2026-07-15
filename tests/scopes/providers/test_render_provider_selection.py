from assistant_agent.config import ProviderConfig
from assistant_agent.services.render_adapter import HttpRenderAdapter, MockRenderAdapter, RenderRequest, create_render_adapter
from assistant_agent.tools.registry import create_default_registry


def test_create_render_adapter_defaults_to_mock() -> None:
    adapter = create_render_adapter(ProviderConfig())

    assert isinstance(adapter, MockRenderAdapter)


def test_create_render_adapter_returns_http_skeleton_when_selected() -> None:
    adapter = create_render_adapter(ProviderConfig(render_provider="http"))

    assert isinstance(adapter, HttpRenderAdapter)


def test_http_render_adapter_missing_config_returns_provider_unconfigured() -> None:
    adapter = create_render_adapter(ProviderConfig(render_provider="http"))

    result = adapter.render(RenderRequest(scene_description="现代办公室"))

    assert result.status == "failed"
    assert result.provider == "http"
    assert result.errors[0]["code"] == "provider_unconfigured"
    assert "RENDER_BASE_URL" in result.errors[0]["message"]


def test_default_registry_uses_selected_render_adapter() -> None:
    registry = create_default_registry(ProviderConfig(render_provider="http"))

    result = registry.run("render_3d", {"scene_description": "现代办公室"})

    assert result.success is False
    assert result.data is not None
    assert result.data["provider"] == "http"
    assert result.data["errors"][0]["code"] == "provider_unconfigured"
