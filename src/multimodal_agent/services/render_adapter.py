"""3D render adapter interface and mock implementation."""

from typing import Protocol

from pydantic import BaseModel

from multimodal_agent.schemas.generation import RenderResult


class RenderInput(BaseModel):
    """Input for a 3D or scene render task."""

    product_id: str | None = None
    image_url: str | None = None
    scene: str | None = None
    material: str | None = None
    lighting: str | None = None
    camera: str | None = None


class RenderAdapter(Protocol):
    """Adapter contract for render backends."""

    def create_render(self, input: RenderInput) -> RenderResult:
        """Create a render task and return structured task output."""


class MockRenderAdapter:
    """Deterministic local render adapter."""

    def create_render(self, input: RenderInput) -> RenderResult:
        if not input.product_id and not input.image_url:
            raise ValueError("缺少商品或图片输入，无法渲染")

        return RenderResult(
            task_id="mock_render_task_1",
            status="succeeded",
            preview_url="local://render/preview.png",
            model_url="local://render/model.glb",
        )
