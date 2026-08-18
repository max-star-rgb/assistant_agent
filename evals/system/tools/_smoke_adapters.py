"""Small deterministic adapters used only by Tool execution smokes."""

from __future__ import annotations

from assistant_agent.media.vision.models import (
    VisionUnderstandingRequest,
    VisionUnderstandingResult,
)
from assistant_agent.tools.plugins.builtin.shopping.models import (
    PriceCompareRequest,
    PriceCompareResult,
    ProductSearchRequest,
    ProductSearchResult,
)


class EmptyShoppingSearchAdapter:
    def search(self, request: ProductSearchRequest) -> ProductSearchResult:
        return ProductSearchResult(
            provider="tool-smoke",
            query_used=request.query,
            output_ref="smoke://shopping/search",
        )


class EmptyShoppingCompareAdapter:
    def compare(self, request: PriceCompareRequest) -> PriceCompareResult:
        return PriceCompareResult(
            query=request.query,
            summary="固定输入比价执行完成。",
            provider="tool-smoke",
            output_ref="smoke://shopping/compare",
        )


class VisualToolSmokeClient:
    def understand(self, request: VisionUnderstandingRequest) -> VisionUnderstandingResult:
        return VisionUnderstandingResult(
            summary="固定视觉媒体输入已完成理解。",
            objects=["测试物体"],
            provider="tool-smoke",
            output_ref="smoke://visual/inspect",
            source=("live_view" if request.video_ids else "request_image"),
            media_kind=("live_view" if request.video_ids else "image"),
            media_refs=[*request.image_ids, *request.video_ids],
        )


__all__ = [
    "EmptyShoppingCompareAdapter",
    "EmptyShoppingSearchAdapter",
    "VisualToolSmokeClient",
]
