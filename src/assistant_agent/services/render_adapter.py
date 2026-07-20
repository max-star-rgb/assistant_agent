"""3D render adapter interface and mock implementation."""

from typing import Any, Protocol

from pydantic import BaseModel, Field

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.generation import RenderResult
from assistant_agent.services.provider_errors import build_provider_error
from assistant_agent.services.tool_manifest import RENDER_3D_CAPABILITY


class RenderRequest(BaseModel):
    """Input contract for a lightweight 3D or scene render request."""

    scene_description: str | None = None
    product_ref: str | None = None
    product_title: str | None = None
    product_image_url: str | None = None
    model_ref: str | None = None
    image_ref: str | None = None
    video_ref: str | None = None
    visual_summary: str | None = None
    video_summary: str | None = None
    style: str | None = None
    camera_angle: str | None = None
    lighting: str | None = None
    output_format: str | None = "preview"
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    user_id: str | None = None
    session_id: str | None = None
    memory_context: list[str] = Field(default_factory=list)

    # Backward-compatible fields used by earlier render tasks.
    product_id: str | None = None
    image_url: str | None = None
    scene: str | None = None
    material: str | None = None
    camera: str | None = None

    def normalized_scene(self) -> str | None:
        return self.scene_description or self.scene

    def used_input_summary(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "scene_description": self.normalized_scene(),
                "product_ref": self.product_ref or self.product_id,
                "product_title": self.product_title,
                "product_image_url": self.product_image_url or self.image_url,
                "model_ref": self.model_ref,
                "image_ref": self.image_ref,
                "video_ref": self.video_ref,
                "visual_summary": self.visual_summary,
                "video_summary": self.video_summary,
                "style": self.style,
                "camera_angle": self.camera_angle or self.camera,
                "lighting": self.lighting,
                "output_format": self.output_format,
                "width": self.width,
                "height": self.height,
            }.items()
            if value
        }


RenderInput = RenderRequest


class RenderAdapter(Protocol):
    """Adapter contract for render backends."""

    def render(self, request: RenderRequest) -> RenderResult:
        """Create a render task and return structured task output."""


class MockRenderAdapter:
    """Deterministic local render adapter."""

    provider = "mock"

    def render(self, request: RenderRequest) -> RenderResult:
        scene = request.normalized_scene()
        if not scene:
            return _failed_result(
                task_id="mock_render_missing_scene",
                provider=self.provider,
                code="render_missing_scene",
                message="Render request requires scene_description or scene.",
                recoverable=True,
            )

        output_ref = "mock://render/preview.png"
        return RenderResult(
            task_id="mock_render_task_1",
            render_id="mock_render_task_1",
            status="succeeded",
            provider=self.provider,
            preview_url=output_ref,
            model_url="mock://render/model.glb",
            output_ref=output_ref,
            scene_description=scene,
            used_inputs=request.used_input_summary(),
            latency_ms=1,
        )

    def create_render(self, input: RenderInput) -> RenderResult:
        """Backward-compatible alias for earlier tasks."""

        return self.render(input)


class HttpRenderAdapter:
    """HTTP render provider skeleton.

    This adapter deliberately does not perform network IO in the default test
    path. It only validates provider configuration and returns structured
    provider state.
    """

    provider = "http"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def render(self, request: RenderRequest) -> RenderResult:
        if not self.base_url:
            return self._unconfigured("RENDER_BASE_URL")
        if not self.api_key:
            return self._unconfigured("RENDER_API_KEY")
        return _failed_result(
            task_id="http_render_provider_unavailable",
            provider=self.provider,
            code="render_provider_unavailable",
            message="HTTP render provider skeleton is configured but no real client is enabled.",
            recoverable=True,
        )

    def create_render(self, input: RenderInput) -> RenderResult:
        """Backward-compatible alias for earlier tasks."""

        return self.render(input)

    def _unconfigured(self, missing: str) -> RenderResult:
        return _failed_result(
            task_id="http_render_unconfigured",
            provider=self.provider,
            code="provider_unconfigured",
            message=f"http render provider is missing {missing}.",
            recoverable=True,
        )


def create_render_adapter(config: ProviderConfig | None = None) -> RenderAdapter:
    """Create a render adapter without initializing real render clients."""

    resolved = config or ProviderConfig.from_env()
    if resolved.render_provider == "http":
        return HttpRenderAdapter(
            base_url=resolved.render_base_url,
            api_key=resolved.render_api_key,
            timeout_seconds=resolved.render_timeout_seconds,
        )
    return MockRenderAdapter()


def _failed_result(
    *,
    task_id: str,
    provider: str,
    code: str,
    message: str,
    recoverable: bool,
) -> RenderResult:
    error = build_provider_error(
        code,
        message,
        recoverable=recoverable,
        provider=provider,
        capability=RENDER_3D_CAPABILITY,
    )
    return RenderResult(
        task_id=task_id,
        render_id=task_id,
        status="failed",
        provider=provider,
        error=f"{error.code}: {error.message}",
        errors=[
            {
                "code": error.code,
                "message": error.message,
                "recoverable": error.recoverable,
            }
        ],
    )
