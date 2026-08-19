"""Image generation Tool backed by a Plugin-private adapter."""

from pathlib import PurePosixPath
from typing import Any

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
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.input_binding import RuntimeInputBinding


class ImageGenerationTool(ToolBase):
    name = IMAGE_GENERATION_TOOL_NAME
    description = (
        "根据文本中的内容、构图和风格要求生成图片；返回可供后续展示或处理的 image_id "
        "及生成结果。会调用图片生成服务；不用于理解、检索或修改现有图片。"
    )
    input_schema = ImageGenerationRequest
    output_schema = ImageGenerationResult
    category = "generate"
    repeat_policy = "once_per_run"
    llm_hidden_input_fields = (
        "size",
        "n",
        "prompt_extend",
        "watermark",
        "style",
        "product_id",
        "product_title",
        "product_info",
        "reference_image_ids",
        "negative_prompt",
        "seed",
        "width",
        "height",
    )
    runtime_input_bindings = (
        RuntimeInputBinding(field="user_id", source="runtime_identity", key="user_id"),
        RuntimeInputBinding(
            field="session_id", source="runtime_identity", key="session_id"
        ),
        RuntimeInputBinding(
            field="memory_context", source="memory_context", key="summaries"
        ),
    )

    def __init__(
        self,
        adapter: ImageGenerationAdapter | None = None,
        *,
        artifact_base_url: str | None = None,
    ) -> None:
        super().__init__()
        self.adapter = adapter or MockImageGenerationAdapter()
        self.artifact_base_url = str(artifact_base_url or "").strip().rstrip("/")

    def _execute(
        self, input: ImageGenerationRequest, context: ToolContext
    ) -> ToolResult:
        try:
            result = self.adapter.generate(input)
            if result.status == "succeeded":
                result = materialize_image_generation_result(result)
                result = _publish_image_ids(result)
        except ProviderAdapterError as exc:
            data, contract = _image_generation_provider_error_contract(exc)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=str(exc),
                data=data,
                model_observation=_image_generation_model_observation(data),
                contract=contract,
            )
        except ValueError as exc:
            data, contract = _image_generation_error_contract(str(exc))
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=str(exc),
                data=data,
                model_observation=_image_generation_model_observation(data),
                contract=contract,
            )
        data, contract = _image_generation_output_contract(
            result,
            artifact_base_url=self.artifact_base_url,
        )
        if result.status == "failed":
            return ToolResult(
                tool_name=self.name,
                success=False,
                data=data,
                model_observation=_image_generation_model_observation(data),
                error=result.error or "image_generation_failed",
                output_ref=result.output_ref,
                latency_ms=result.latency_ms,
                contract=contract,
            )

        return ToolResult(
            tool_name=self.name,
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
        "image_id": data.get("image_id"),
        "errors": data.get("errors"),
    }
    if not data.get("image_id"):
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
