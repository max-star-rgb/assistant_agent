"""Vision understanding tool backed by an adapter."""

from typing import Any

from assistant_agent.schemas.perception import VisualUnderstandingResult
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.schemas.capability_output import build_capability_output_contract
from assistant_agent.services.vision_adapter import (
    MockVisionUnderstandingAdapter,
    VisionUnderstandingAdapter,
    VisionUnderstandingInput,
)
from assistant_agent.services.provider_errors import (
    ProviderAdapterError,
    build_provider_error,
)
from assistant_agent.tools.base import MockTool, ToolContext


class VisionUnderstandingTool(MockTool):
    name = "vision_understanding"
    description = "Image and video understanding through a vision adapter."
    input_schema = VisionUnderstandingInput
    output_schema = VisualUnderstandingResult

    def __init__(self, adapter: VisionUnderstandingAdapter | None = None) -> None:
        self.adapter = adapter or MockVisionUnderstandingAdapter()

    def _run(self, input: VisionUnderstandingInput, context: ToolContext) -> ToolResult:
        try:
            result = self.adapter.understand(input)
        except ProviderAdapterError as exc:
            capability = (
                "video_understanding" if input.video_ids else "image_understanding"
            )
            provider = getattr(
                getattr(self.adapter, "config", None), "provider", "unknown"
            )
            error = build_provider_error(
                exc.code, exc.message, provider=provider, capability=capability
            )
            contract = build_capability_output_contract(
                capability=capability,
                status="failed",
                errors=[error.model_dump(mode="json")],
            )
            return ToolResult(
                tool_name=self.name,
                success=False,
                model_observation=_vision_error_model_observation(
                    error.model_dump(mode="json")
                ),
                error=f"{error.code}: {error.message}",
                contract=contract,
            )
        except ValueError as exc:
            message = build_provider_error(
                "provider_request_invalid", str(exc), recoverable=True
            ).message
            contract = build_capability_output_contract(
                capability="video_understanding"
                if input.video_ids
                else "image_understanding",
                status="failed",
                errors=[
                    {
                        "code": "missing_required_input",
                        "message": message,
                        "recoverable": True,
                    }
                ],
            )
            return ToolResult(
                tool_name=self.name,
                success=False,
                model_observation={
                    "summary": message,
                    "errors": [
                        {
                            "code": "missing_required_input",
                            "message": message,
                            "recoverable": True,
                        }
                    ],
                },
                error=message,
                contract=contract,
            )

        output_ref = _vision_output_ref(self.adapter)
        capability = "video_understanding" if input.video_ids else "image_understanding"
        data = result.model_dump(mode="json")
        contract = build_capability_output_contract(
            capability=capability,
            status="succeeded",
            output_ref=output_ref,
            data=data,
            metadata={
                "provider": getattr(
                    getattr(self.adapter, "config", None), "provider", "mock"
                ),
                "latency_ms": 1,
            },
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation=_vision_model_observation(data),
            output_ref=output_ref,
            latency_ms=1,
            contract=contract,
        )


def _vision_output_ref(adapter: VisionUnderstandingAdapter) -> str:
    config = getattr(adapter, "config", None)
    provider = getattr(config, "provider", None)
    if provider:
        return f"provider://vision/{provider}"
    return "mock://vision/white-low-top-sneaker"


def _vision_model_observation(data: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "summary",
        "objects",
        "colors",
        "materials",
        "scene",
        "style_tags",
        "text_in_media",
    )
    return {key: data[key] for key in keys if data.get(key) not in (None, "", [], {})}


def _vision_error_model_observation(error: dict[str, Any]) -> dict[str, Any]:
    message = str(error.get("message") or "Vision understanding failed.")
    return {"summary": message, "errors": [error]}
