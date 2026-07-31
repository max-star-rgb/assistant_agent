"""Offline contracts for the direct real-shopping system eval runner."""

import importlib
import importlib.util
from pathlib import Path

from assistant_agent.config import ProviderConfig
from assistant_agent.tools.plugins.builtin.shopping.models import (
    PriceCompareResult,
    ProductResult,
    ProductSearchResult,
)
from assistant_agent.tools.plugins.builtin.shopping.tool import ShoppingSearchTool
from assistant_agent.tools.registry import ToolRegistry
from evals.system.tools import runner


class _SearchAdapter:
    def __init__(self, *, source: str = "haodanku", url: str | None = None) -> None:
        self.source = source
        self.url = url or "https://detail.example.test/item/1"

    def search(self, request) -> ProductSearchResult:
        product = ProductResult(
            product_id="real-contract-item",
            title="抽纸",
            price=19.9,
            effective_price=16.9,
            platform="taobao",
            product_url=self.url,
            source=self.source,
        )
        return ProductSearchResult(
            items=[product],
            provider="haodanku",
            query_used=request.query,
            total=1,
            output_ref="haodanku://search/paper",
        )


class _CompareAdapter:
    def compare(self, request) -> PriceCompareResult:
        return PriceCompareResult(
            query=request.query,
            items=list(request.items),
            summary="取得一个可购买候选。",
            provider="haodanku",
        )


def _registry(*, source: str = "haodanku", url: str | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ShoppingSearchTool(
            search_adapter=_SearchAdapter(source=source, url=url),
            compare_adapter=_CompareAdapter(),
        )
    )
    registry.seal()
    return registry


def _real_config() -> ProviderConfig:
    return ProviderConfig(
        provider_mode="real",
        chat_provider="openai",
        chat_adapter_kind="openai",
        openai_api_key="test-only",
        shopping_search_provider="haodanku",
        shopping_compare_provider="haodanku",
        haodanku_api_key="test-only",
    )


def test_direct_shopping_eval_runs_governed_tool_and_accepts_real_result(
    tmp_path: Path,
) -> None:
    run_eval = getattr(runner, "run_direct_shopping_system_eval", None)
    assert callable(run_eval), "direct shopping system eval runner is missing"

    result = run_eval(
        config=_real_config(),
        allow_real_tools=True,
        keyword="纸巾",
        output_root=tmp_path,
        registry=_registry(),
    )

    assert result.passed is True
    assert result.checks["shopping_tool_registered"] is True
    assert result.action_validation_code == "accepted"
    assert result.tool_call_status == "succeeded"
    assert result.provider == "haodanku"
    assert result.outcome == "success"
    assert result.selection_count == 1
    assert result.product_sources == ["haodanku"]
    assert result.product_links == ["https://detail.example.test/item/1"]
    assert result.artifact.result_path.is_file()
    assert result.artifact.summary_path.is_file()


def test_direct_shopping_eval_rejects_non_real_product_source(
    tmp_path: Path,
) -> None:
    run_eval = getattr(runner, "run_direct_shopping_system_eval", None)
    assert callable(run_eval), "direct shopping system eval runner is missing"

    result = run_eval(
        config=_real_config(),
        allow_real_tools=True,
        keyword="纸巾",
        output_root=tmp_path,
        registry=_registry(source="unknown"),
    )

    assert result.passed is False
    assert result.checks["real_product_sources"] is False
    assert "real_product_sources" in result.failures


def test_direct_shopping_eval_cli_fails_closed_without_operator_confirmation(
    capsys,
) -> None:
    module_spec = importlib.util.find_spec("scripts.run_system_shopping_eval")
    assert module_spec is not None, "direct shopping system eval CLI is missing"
    cli = importlib.import_module("scripts.run_system_shopping_eval")

    exit_code = cli.main(["--no-env-file"])

    assert exit_code == 2
    payload = capsys.readouterr().out
    assert "direct_shopping_eval_not_authorized" in payload
