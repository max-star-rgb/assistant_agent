from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from assistant_agent.schemas.tool_observation import observation_from_tool_result


REPO_ROOT = Path(__file__).resolve().parents[3]
ATTACHED_IMAGE_1 = Path(
    os.environ.get(
        "ASSISTANT_AGENT_ATTACHED_IMAGE_1",
        "/home/lenovo1/图片/0717619c-e0de-4fe7-b21c-50b6754eb8b8.png",
    )
)
DOWNSAMPLED_IMAGE_1_JPEG = (
    REPO_ROOT / ".local" / "integration" / "tools" / "image-1-cake-480p.jpg"
)


def skip_unless_manual_tool_provider_smoke_selected(
    test_file: str | Path,
    *,
    env_var: str,
) -> None:
    if manual_tool_provider_smoke_selected(test_file, env_var=env_var):
        return
    pytest.skip(
        f"run this test file directly, or set {env_var}=1 for this real provider smoke"
    )


def manual_tool_provider_smoke_selected(
    test_file: str | Path,
    *,
    env_var: str,
) -> bool:
    if os.environ.get(env_var) == "1":
        return True
    if os.environ.get("ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED_FILE_COUNT") != "1":
        return False
    selected_file = os.environ.get("ASSISTANT_AGENT_REAL_PROVIDER_TOOL_SELECTED_FILE")
    if not selected_file:
        return False
    return Path(selected_file).resolve() == Path(test_file).resolve()


def attached_image_1_as_jpeg() -> Path:
    if _is_jpeg(DOWNSAMPLED_IMAGE_1_JPEG):
        return DOWNSAMPLED_IMAGE_1_JPEG
    if not ATTACHED_IMAGE_1.is_file():
        pytest.skip(f"Image #1 is not available at {ATTACHED_IMAGE_1}")
    DOWNSAMPLED_IMAGE_1_JPEG.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "/usr/bin/ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(ATTACHED_IMAGE_1),
        "-frames:v",
        "1",
        "-vf",
        "scale=480:480:force_original_aspect_ratio=decrease",
        "-vcodec",
        "mjpeg",
        "-q:v",
        "6",
        str(DOWNSAMPLED_IMAGE_1_JPEG),
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True)
    except OSError:
        pytest.skip(
            "/usr/bin/ffmpeg is required to convert Image #1 to the JPEG VLM input"
        )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace")
        pytest.fail(f"failed to downsample Image #1 to 480p JPEG: {message}")
    return DOWNSAMPLED_IMAGE_1_JPEG


def print_json_section(title: str, value: Any) -> None:
    print(f"=== {title} ===", flush=True)
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def print_text_section(title: str, text: str | None, *, empty: str) -> None:
    print(f"=== {title} ===", flush=True)
    print(text or empty, flush=True)


def filtered_tool_result_for_llm(result: Any) -> dict[str, object]:
    observation = observation_from_tool_result(result)
    return observation.model_dump(mode="json")


def visual_tool_success_layers(
    result: Any,
    *,
    raw_text_received: bool | None = None,
    snapshot_source: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, object]:
    data = result.data if isinstance(result.data, dict) else {}
    errors = data.get("errors")
    data_errors = errors if isinstance(errors, list) else []
    summary = data.get("summary")
    source = data.get("source")
    execution_success = result.success is True
    semantic_success = (
        execution_success
        and isinstance(summary, str)
        and bool(summary.strip())
        and not data_errors
    )
    layers: dict[str, object] = {
        "execution_success": execution_success,
        "semantic_success": semantic_success,
        "tool_success": result.success,
        "contract_status": result.contract.status
        if result.contract is not None
        else None,
        "source": source,
        "error": result.error,
        "data_errors": data_errors,
    }
    if snapshot_source is not None:
        layers["snapshot_publishable"] = semantic_success and source == snapshot_source
    if raw_text_received is not None:
        layers["raw_text_received"] = raw_text_received
    if extra:
        layers.update(extra)
    return layers


def proxy_env_names() -> list[str]:
    return sorted(
        key
        for key in os.environ
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    )


def _is_jpeg(path: Path) -> bool:
    if not path.is_file():
        return False
    data = path.read_bytes()
    return data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")
