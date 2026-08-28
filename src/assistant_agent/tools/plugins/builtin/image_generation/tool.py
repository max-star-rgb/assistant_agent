"""Image generation Tool backed by a Plugin-private adapter."""

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Annotated, Any

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.tools.plugins.builtin.image_generation.models import (
    GeneratedImageArtifact,
    ImageGenerationRequest,
    ImageGenerationResult,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.capability_output import (
    CapabilityOutputContract,
    build_text_capability_output,
)
from assistant_agent.providers.provider_errors import ProviderAdapterError
from assistant_agent.tools.plugins.builtin.image_generation.backend import (
    ImageGenerationAdapter,
    MockImageGenerationAdapter,
)
from assistant_agent.runtime.generated_artifacts import (
    GENERATED_ARTIFACT_PUBLIC_PREFIX,
    MAX_DELIVERED_IMAGE_COUNT,
    generated_artifact_payload,
    materialize_image_generation_result,
)
from assistant_agent.tools.ids import (
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_GENERATION_TOOL_NAME,
)
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    invoke_native_tool,
)


def create_image_generation_tool(
    adapter: ImageGenerationAdapter | None = None,
    *,
    artifact_base_url: str | None = None,
) -> BaseTool:
    """Create the native image generation Tool."""

    image_adapter = adapter or MockImageGenerationAdapter()
    public_artifact_base_url = str(artifact_base_url or "").strip().rstrip("/")

    @tool(IMAGE_GENERATION_TOOL_NAME, response_format="content_and_artifact")
    def image_generation(
        prompt: Annotated[
            str,
            Field(
                min_length=1,
                description="图片内容、构图、风格和关键视觉要求。",
            ),
        ],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """根据文本中的内容、构图和风格要求生成图片。

        返回可供后续展示或处理的`image_id`和`url`及生成结果。会调用图片生成服务；
        不用于理解、检索或修改现有图片。
        """

        return invoke_native_tool(
            IMAGE_GENERATION_TOOL_NAME,
            lambda: _execute_image_generation_from_runtime(
                image_adapter,
                prompt,
                runtime,
                artifact_base_url=public_artifact_base_url,
            ),
        )

    return configure_builtin_tool(image_generation, "generate")


def _execute_image_generation_from_runtime(
    adapter: ImageGenerationAdapter,
    prompt: str,
    runtime: ToolRuntime[AssistantRunContext],
    *,
    artifact_base_url: str = "",
) -> ToolResult:
    state = runtime.state if isinstance(runtime.state, Mapping) else {}
    request = ImageGenerationRequest(
        prompt=prompt,
        user_id=authenticated_user_identity(runtime),
        session_id=runtime.execution_info.thread_id,
        memory_context=list(state.get("memory_context", ())),
    )
    return _execute_image_generation(
        adapter,
        request,
        artifact_base_url=artifact_base_url,
    )


def _execute_image_generation(
    adapter: ImageGenerationAdapter,
    input: ImageGenerationRequest,
    *,
    artifact_base_url: str = "",
) -> ToolResult:
    try:
        result = adapter.generate(input)
        if result.status == "succeeded":
            result = materialize_image_generation_result(result)
            result = _publish_image_ids(result)
    except ProviderAdapterError as exc:
        data, contract = _image_generation_provider_error_contract(exc)
        return ToolResult(
            tool_name=IMAGE_GENERATION_TOOL_NAME,
            success=False,
            error=str(exc),
            data=data,
            model_observation=_image_generation_model_observation(data),
            contract=contract,
        )
    except ValueError as exc:
        data, contract = _image_generation_error_contract(str(exc))
        return ToolResult(
            tool_name=IMAGE_GENERATION_TOOL_NAME,
            success=False,
            error=str(exc),
            data=data,
            model_observation=_image_generation_model_observation(data),
            contract=contract,
        )
    data, contract = _image_generation_output_contract(
        result,
        artifact_base_url=artifact_base_url,
    )
    if result.status == "failed":
        return ToolResult(
            tool_name=IMAGE_GENERATION_TOOL_NAME,
            success=False,
            data=data,
            model_observation=_image_generation_model_observation(data),
            error=result.error or "image_generation_failed",
            output_ref=result.output_ref,
            latency_ms=result.latency_ms,
            contract=contract,
        )

    return ToolResult(
        tool_name=IMAGE_GENERATION_TOOL_NAME,
        success=True,
        data=data,
        model_observation=_image_generation_model_observation(data),
        output_ref=result.output_ref or result.image_url,
        latency_ms=result.latency_ms or 1,
        contract=contract,
    )


def _image_generation_output_contract(
    result: ImageGenerationResult,
    *,
    artifact_base_url: str,
) -> tuple[dict, CapabilityOutputContract]:
    data = {
        "task_id": result.task_id,
        "image_id": result.image_id,
        "request_id": result.request_id,
        "prompt": result.prompt,
        "prompt_used": result.prompt_used or result.prompt,
        "provider": result.provider,
        "model": result.model,
        "images": [
            image.model_dump(exclude_none=True)
            for image in _generated_image_artifacts(
                result,
                artifact_base_url=artifact_base_url,
            )
        ],
    }
    public = build_text_capability_output(
        capability=IMAGE_GENERATION_CAPABILITY,
        status=result.status,
        output_ref=result.output_ref or result.image_url,
        data=data,
        errors=result.errors,
    )
    payload = {
        **data,
        "status": result.status,
        "errors": result.errors,
        "contract": public,
    }
    return payload, CapabilityOutputContract.model_validate(public)


def _generated_image_artifacts(
    result: ImageGenerationResult,
    *,
    artifact_base_url: str,
) -> list[GeneratedImageArtifact]:
    refs = result.download_urls or (
        [result.download_url] if result.download_url else []
    )
    if not refs and result.output_ref:
        refs = [result.output_ref]
    images: list[GeneratedImageArtifact] = []
    seen: set[str] = set()
    for ref in refs:
        if not _is_managed_generated_ref(ref) or ref in seen:
            continue
        path = PurePosixPath(ref)
        images.append(
            GeneratedImageArtifact(
                image_id=path.stem,
                output_ref=ref,
                url=f"{artifact_base_url}{ref}" if artifact_base_url else None,
                mime_type=_image_mime_type(path.suffix),
            )
        )
        seen.add(ref)
        if len(images) >= MAX_DELIVERED_IMAGE_COUNT:
            break
    return images


def _is_managed_generated_ref(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    prefix = GENERATED_ARTIFACT_PUBLIC_PREFIX.rstrip("/") + "/"
    if not value.startswith(prefix):
        return False
    filename = value.removeprefix(prefix)
    return bool(filename) and PurePosixPath(filename).name == filename


def _image_mime_type(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix.lower(), "image/png")


def _image_generation_error_contract(
    message: str,
) -> tuple[dict, CapabilityOutputContract]:
    public = build_text_capability_output(
        capability=IMAGE_GENERATION_CAPABILITY,
        status="failed",
        errors=[
            {"code": "missing_required_input", "message": message, "recoverable": True}
        ],
    )
    return {
        "status": "failed",
        "errors": public["errors"],
        "contract": public,
    }, CapabilityOutputContract.model_validate(public)


def _image_generation_provider_error_contract(
    error: ProviderAdapterError,
) -> tuple[dict, CapabilityOutputContract]:
    public = build_text_capability_output(
        capability=IMAGE_GENERATION_CAPABILITY,
        status="failed",
        errors=[{"code": error.code, "message": error.message, "recoverable": False}],
    )
    return {
        "status": "failed",
        "errors": public["errors"],
        "contract": public,
    }, CapabilityOutputContract.model_validate(public)


def _image_generation_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "images": [
            {"image_id": image.get("image_id"), "url": image.get("url")}
            for image in data.get("images", [])
            if isinstance(image, Mapping)
            and image.get("image_id")
            and image.get("url")
        ],
        "errors": data.get("errors"),
    }
    if not observation["images"]:
        observation["summary"] = _image_generation_summary(data)
    return {
        key: value
        for key, value in observation.items()
        if value not in (None, "", [], {})
    }


def _image_generation_summary(data: dict[str, Any]) -> str:
    if data.get("status") == "succeeded":
        return "Image generation succeeded."
    errors = data.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "Image generation failed.")
    return "Image generation failed."


def _publish_image_ids(
    result: ImageGenerationResult,
) -> ImageGenerationResult:
    refs = result.download_urls or (
        [result.download_url] if result.download_url else []
    )
    if not refs and result.output_ref:
        refs = [result.output_ref]
    image_ids: list[str] = list(result.image_id)
    for ref in refs:
        payload = generated_artifact_payload(ref)
        if payload is None:
            continue
        image_id = payload.image_id.rsplit(".", 1)[0]
        if not image_id:
            continue
        image_ids.append(image_id)
    return result.model_copy(update={"image_id": list(dict.fromkeys(image_ids))})
