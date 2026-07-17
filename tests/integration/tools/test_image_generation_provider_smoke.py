from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.generation import ImageGenerationInput
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.services.image_generation_adapter import (
    create_image_generation_adapter,
)
from assistant_agent.services.generated_artifacts import (
    GENERATED_ARTIFACT_DIR,
    GENERATED_ARTIFACT_PUBLIC_PREFIX,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.image_generation_tool import ImageGenerationTool
from tests.tool_smoke_metrics import build_tool_smoke_metrics, measure_tool_run


THIS_FILE = Path(__file__).resolve()


def _configured_image_generation_tool() -> tuple[ImageGenerationTool, ProviderConfig]:
    _skip_unless_manual_tool_selected("RUN_REAL_IMAGE_GENERATION_TOOL_TEST")
    env = {
        **os.environ,
        "MULTIMODAL_AGENT_RUNTIME_PROFILE": os.environ.get(
            "MULTIMODAL_AGENT_RUNTIME_PROFILE", "provider_smoke"
        ),
        "MULTIMODAL_AGENT_IMAGE_PROVIDER": os.environ.get(
            "MULTIMODAL_AGENT_IMAGE_PROVIDER", "qwen"
        ),
    }
    config = ProviderConfig.from_env(env)
    if config.runtime_profile.name not in {"provider_smoke", "pilot"}:
        pytest.skip("set MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke or pilot")
    provider = config.resolved_image_generation_provider()
    if provider.provider == "mock":
        pytest.skip("set MULTIMODAL_AGENT_IMAGE_PROVIDER=qwen or ark")
    if provider.adapter_kind not in {"dashscope_image", "ark_image"}:
        pytest.skip(f"{provider.provider} image generation adapter is not implemented")
    missing = provider.missing_required_env()
    if missing:
        pytest.skip(
            f"set {', '.join(missing)} for {provider.provider} image generation"
        )
    return ImageGenerationTool(adapter=create_image_generation_adapter(config)), config


def test_real_image_generation_tool_provider_smoke(capsys) -> None:
    tool, config = _configured_image_generation_tool()

    result, tool_elapsed_ms = measure_tool_run(
        lambda: tool.run(
            ImageGenerationInput(
                prompt="一张简洁的正方形应用图标，白色背景，中间是清晰的蓝色数字 1，扁平设计。",
                n=1,
                watermark=False,
            ),
            ToolContext(),
        )
    )

    with capsys.disabled():
        print()
        _print_json_section(
            "IMAGE GENERATION PROVIDER CONFIG",
            _provider_config_diagnostics(config),
        )
        _print_text_section(
            "IMAGE GENERATION RAW OUTPUT",
            None,
            empty="<raw provider payload is not exposed by this tool>",
        )
        _print_json_section("TOOL RESULT", result.model_dump(mode="json"))
        _print_json_section(
            "TOOL SMOKE METRICS",
            build_tool_smoke_metrics(result, tool_elapsed_ms=tool_elapsed_ms),
        )
        _print_json_section("LOCAL ARTIFACT FILES", _local_artifact_files(result))
        _print_json_section(
            "FILTERED TOOL RESULT (LLM-FACING)",
            _filtered_tool_result_for_llm(result),
        )
        _print_json_section("SUCCESS LAYERS", _success_layers(result))

    layers = _success_layers(result)
    assert layers["execution_success"] is True, layers
    assert layers["semantic_success"] is True, layers


def _success_layers(result) -> dict[str, object]:
    data = result.data if isinstance(result.data, dict) else {}
    image_urls = (
        data.get("image_urls") if isinstance(data.get("image_urls"), list) else []
    )
    image_url = data.get("image_url")
    errors = data.get("errors")
    data_errors = errors if isinstance(errors, list) else []
    execution_success = result.success is True
    semantic_success = (
        execution_success
        and data.get("status") == "succeeded"
        and bool(image_url or image_urls)
        and not data_errors
    )
    return {
        "execution_success": execution_success,
        "semantic_success": semantic_success,
        "tool_success": result.success,
        "contract_status": result.contract.status if result.contract else None,
        "status": data.get("status"),
        "output_ref": result.output_ref,
        "image_url_received": bool(image_url),
        "image_url_count": len(image_urls),
        "error": result.error,
        "data_errors": data_errors,
    }


def _provider_config_diagnostics(config: ProviderConfig) -> dict[str, object]:
    provider = config.resolved_image_generation_provider()
    return {
        "python": sys.executable,
        "cwd": str(Path.cwd()),
        "runtime_profile": config.runtime_profile.name,
        "image_provider": provider.provider,
        "adapter_kind": provider.adapter_kind,
        "base_url": provider.base_url,
        "model": provider.model,
        "credential_source": provider.spec.api_key_env,
        "proxy_env_names": _proxy_env_names(),
    }


def _local_artifact_files(result) -> list[dict[str, object]]:
    data = result.data if isinstance(result.data, dict) else {}
    candidates: list[str] = []
    for key in ("output_ref", "image_url", "download_url"):
        value = data.get(key)
        if isinstance(value, str):
            candidates.append(value)
    for key in ("image_urls", "download_urls"):
        values = data.get(key)
        if isinstance(values, list):
            candidates.extend(value for value in values if isinstance(value, str))

    files: list[dict[str, object]] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = _local_path_for_generated_artifact(candidate)
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        exists = path.is_file()
        files.append(
            {
                "artifact_url": candidate,
                "path": str(path),
                "exists": exists,
                "size_bytes": path.stat().st_size if exists else None,
            }
        )
    return files


def _local_path_for_generated_artifact(value: str) -> Path | None:
    prefix = f"{GENERATED_ARTIFACT_PUBLIC_PREFIX.rstrip('/')}/"
    if not value.startswith(prefix):
        return None
    filename = value.removeprefix(prefix)
    if not filename or "/" in filename:
        return None
    return GENERATED_ARTIFACT_DIR / filename


def _skip_unless_manual_tool_selected(env_var: str) -> None:
    if _manual_tool_selected(env_var):
        return
    pytest.skip(f"run this test file directly, or set {env_var}=1")


def _manual_tool_selected(env_var: str) -> bool:
    if os.environ.get(env_var) == "1":
        return True
    if os.environ.get("ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED_FILE_COUNT") != "1":
        return False
    selected_file = os.environ.get("ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED_FILE")
    return bool(selected_file and Path(selected_file).resolve() == THIS_FILE)


def _filtered_tool_result_for_llm(result) -> dict[str, object]:
    return observation_from_tool_result(result).model_dump(mode="json")


def _print_json_section(title: str, value: object) -> None:
    print(f"=== {title} ===", flush=True)
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def _print_text_section(title: str, text: str | None, *, empty: str) -> None:
    print(f"=== {title} ===", flush=True)
    print(text or empty, flush=True)


def _proxy_env_names() -> list[str]:
    return sorted(
        key
        for key in os.environ
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    )
