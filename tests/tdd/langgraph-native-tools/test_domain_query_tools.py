from __future__ import annotations

import ast
import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from langchain.agents import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools.plugins.builtin.lodging.models import (
    LodgingOffer,
    LodgingSearchRequest,
    LodgingSearchResult,
)
from assistant_agent.tools.plugins.builtin.lodging.tool import create_lodging_search_tool
from assistant_agent.tools.plugins.builtin.shopping.models import (
    PriceCompareRequest,
    PriceCompareResult,
    ProductResult,
    ProductSearchRequest,
    ProductSearchResult,
)
from assistant_agent.tools.plugins.builtin.shopping.tool import create_shopping_search_tool
from assistant_agent.tools.plugins.builtin.visual_image_search.models import (
    VisualImageSearchProviderError,
    VisualImageSearchRequest,
    VisualImageSearchResult,
)
from assistant_agent.tools.plugins.builtin.visual_image_search.tool import (
    create_visual_image_search_tool,
)


DOMAIN_QUERY_TOOL_MODULES = (
    "assistant_agent.tools.plugins.builtin.shopping.tool",
    "assistant_agent.tools.plugins.builtin.lodging.tool",
    "assistant_agent.tools.plugins.builtin.visual_image_search.tool",
)


class _User(dict):
    identity = "user-sentinel"
    permissions = ()


class _ShoppingSearchAdapter:
    def search(self, request: ProductSearchRequest) -> ProductSearchResult:
        return ProductSearchResult(
            items=[
                ProductResult(
                    product_id="bottle-001",
                    title="Offline bottle",
                    price=39.0,
                    effective_price=35.0,
                    platform="offline-shop",
                    product_url="https://offline.test/bottle-001",
                )
            ],
            provider="offline-shopping",
            query_used=request.query,
            total=1,
            latency_ms=1,
            output_ref="offline://shopping/search",
        )


class _ShoppingCompareAdapter:
    def compare(self, request: PriceCompareRequest) -> PriceCompareResult:
        return PriceCompareResult(
            query=request.query,
            summary="Offline price comparison.",
            provider="offline-shopping",
            items=request.items,
            comparison_status="candidates_only",
            latency_ms=1,
            output_ref="offline://shopping/compare",
        )


class _LodgingAdapter:
    def __init__(self, result: LodgingSearchResult) -> None:
        self.result = result

    def search(self, request: LodgingSearchRequest) -> LodgingSearchResult:
        return self.result


class _FailingVisualImageSearchAdapter:
    def search(self, request: VisualImageSearchRequest) -> VisualImageSearchResult:
        return VisualImageSearchResult(
            image_used=request.image_url or request.image_ids[0],
            provider="offline-visual",
            errors=[
                VisualImageSearchProviderError(
                    code="visual_provider_error",
                    message="api_key=secret-sentinel path=/home/private/image.json",
                )
            ],
        )


def test_shopping_toolnode_keeps_candidates_and_selection_in_artifact() -> None:
    message = _invoke(
        create_shopping_search_tool(
            search_adapter=_ShoppingSearchAdapter(),
            compare_adapter=_ShoppingCompareAdapter(),
        ),
        {"needs": [{"keyword": "water bottle", "quantity": 2}]},
    )

    content = json.loads(message.content[0]["text"])
    assert content["schema_version"] == "shopping_observation_v1"
    assert "needs" not in content
    assert "selections" not in content
    assert content["results"][0]["selected"]["product_id"] == "bottle-001"
    assert message.artifact["needs"][0]["candidates"][0]["product_id"] == "bottle-001"
    assert message.artifact["selections"][0]["subtotal"] == 70.0
    assert message.status == "success"


@pytest.mark.parametrize(
    ("result", "expected_content", "expected_status"),
    [
        (
            LodgingSearchResult(
                success=True,
                provider="offline-lodging",
                offers=[
                    LodgingOffer(
                        offer_id="stay-001",
                        property_name="Offline Inn",
                        nightly_price=200,
                        total_price=400,
                        currency="CNY",
                        source_ref="offline://lodging/stay-001",
                    )
                ],
                observed_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
                output_ref="offline://lodging/search",
                provider_notice="Offline data.",
            ),
            {
                "status": "succeeded",
                "offers": [
                    {
                        "offer_id": "stay-001",
                        "property_name": "Offline Inn",
                        "nightly_price": 200.0,
                        "total_price": 400.0,
                        "currency": "CNY",
                        "price_basis": "quoted_total",
                        "refundable": None,
                        "source_ref": "offline://lodging/stay-001",
                        "address": None,
                        "latitude": None,
                        "longitude": None,
                        "star": None,
                        "score": None,
                        "review": None,
                        "image_url": None,
                        "booking_url": None,
                    }
                ],
                "observed_at": "2026-09-02T00:00:00Z",
                "provider_notice": "Offline data.",
            },
            "success",
        ),
        (
            LodgingSearchResult(
                success=False,
                provider="offline-lodging",
                observed_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
                error_code="provider_timeout",
                error_message="Offline lodging timed out.",
            ),
            "Offline lodging timed out.",
            "error",
        ),
    ],
)
def test_lodging_toolnode_preserves_success_and_failure_structure(
    result: LodgingSearchResult,
    expected_content: dict[str, object] | str,
    expected_status: str,
) -> None:
    message = _invoke(
        create_lodging_search_tool(_LodgingAdapter(result)),
        {
            "destination": "Shanghai",
            "check_in": date(2026, 9, 10).isoformat(),
            "check_out": date(2026, 9, 12).isoformat(),
        },
    )

    assert message.status == expected_status
    if expected_status == "success":
        assert json.loads(message.content[0]["text"]) == expected_content
        assert message.artifact == result.model_dump(mode="json")
    else:
        assert message.content == expected_content
        assert message.artifact is None


def test_visual_image_search_toolnode_sanitizes_provider_error() -> None:
    message = _invoke(
        create_visual_image_search_tool(_FailingVisualImageSearchAdapter()),
        {"image_url": "https://offline.test/source.jpg"},
    )

    assert message.content == "visual_provider_error: [redacted] path=[redacted]"
    assert message.artifact is None
    assert message.status == "error"


@pytest.mark.parametrize("module_name", DOMAIN_QUERY_TOOL_MODULES)
def test_domain_query_tool_modules_do_not_import_compatibility_execution_types(
    module_name: str,
) -> None:
    imported = _imported_names(module_name)

    assert "ToolContext" not in imported
    assert "ToolResult" not in imported
    assert "invoke_native_tool" not in imported


def _invoke(tool: BaseTool, args: dict[str, object]) -> ToolMessage:
    builder = StateGraph(AgentState, context_schema=AssistantRunContext)
    builder.add_node("tools", ToolNode([tool], handle_tool_errors=lambda error: str(error)))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    result = asyncio.run(
        builder.compile().ainvoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": tool.name,
                                "args": args,
                                "id": f"call-{tool.name}",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
            context=AssistantRunContext(),
            config={
                "configurable": {
                    "assistant_id": "assistant-sentinel",
                    "graph_id": "graph-sentinel",
                    "thread_id": "thread",
                    "run_id": "run",
                    "langgraph_auth_user": _User(),
                }
            },
        )
    )
    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    return message


def _imported_names(module_name: str) -> set[str]:
    module_path = Path(__file__).parents[3] / "src" / (module_name.replace(".", "/") + ".py")
    names: set[str] = set()
    for node in ast.walk(ast.parse(module_path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names
