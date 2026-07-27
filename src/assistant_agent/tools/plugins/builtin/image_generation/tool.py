"""Image generation Tool backed by a Plugin-private adapter."""

from typing import Any

from assistant_agent.tools.plugins.builtin.image_generation.models import (
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
    materialize_image_generation_result,
)
from assistant_agent.tools.ids import IMAGE_GENERATION_CAPABILITY, IMAGE_GENERATION_TOOL_NAME
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.input_binding import RuntimeInputBinding


class ImageGenerationTool(ToolBase):
    name = IMAGE_GENERATION_TOOL_NAME
    description = "根据必填的文本提示词生成图片；其他参数仅在用户明确指定时传入，否则由工具使用默认值。"
    input_schema = ImageGenerationRequest
    output_schema = ImageGenerationResult
    category = "generate"
    llm_hidden_input_fields = ("prompt_extend", "watermark")
    runtime_input_bindings = (
        RuntimeInputBinding(field="user_id", source="runtime_identity", key="user_id"),
        RuntimeInputBinding(field="session_id", source="runtime_identity", key="session_id"),
        RuntimeInputBinding(field="memory_context", source="memory_context", key="summaries"),
    )

    def __init__(self, adapter: ImageGenerationAdapter | None = None) -> None:
        self.adapter = adapter or MockImageGenerationAdapter()

    def _run(self, input: ImageGenerationRequest, context: ToolContext) -> ToolResult:
        try:
            result = self.adapter.generate(input)
            if result.status == "succeeded":
                result = materialize_image_generation_result(result)
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
        data, contract = _image_generation_output_contract(result)
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
) -> tuple[dict, CapabilityOutputContract]:
    data = {
        "task_id": result.task_id,
        "image_url": result.image_url,
        "image_urls": result.image_urls
        or ([result.image_url] if result.image_url else []),
        "download_url": result.download_url,
        "download_urls": result.download_urls,
        "request_id": result.request_id,
        "prompt": result.prompt,
        "prompt_used": result.prompt_used or result.prompt,
        "provider": result.provider,
        "model": result.model,
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
        "output_ref": result.output_ref or result.image_url,
        "errors": result.errors,
        "contract": public,
    }
    return payload, CapabilityOutputContract.model_validate(public)


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
    image_urls = data.get("image_urls") or (
        [data["image_url"]] if data.get("image_url") else []
    )
    observation: dict[str, Any] = {
        "status": data.get("status"),
        "summary": _image_generation_summary(data),
        "image_url": data.get("image_url"),
        "image_urls": image_urls,
        "output_ref": data.get("output_ref") or data.get("image_url"),
        "prompt_used": data.get("prompt_used") or data.get("prompt"),
        "errors": data.get("errors"),
    }
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
