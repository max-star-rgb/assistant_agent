"""Manual smoke entry point for the unified shopping_search capability."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.config import ProviderConfig
from assistant_agent.services.product_adapter import (
    create_price_compare_adapter,
    create_product_search_adapter,
)
from assistant_agent.tools.shopping_search_tool import ShoppingSearchTool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a manual shopping_search smoke test. Defaults use offline mock adapters.",
    )
    parser.add_argument("--query", required=True, help="Text query for shopping search.")
    parser.add_argument("--budget-max", type=float, default=None, help="Optional maximum offer price.")
    parser.add_argument("--top-k", type=int, default=5, help="Maximum number of products/offers to return.")
    parser.add_argument("--local-json", default=None, help="Small local JSON product dataset for local_json provider.")
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = _normalized_env(os.environ if env is None else env, args.local_json)

    missing = _missing_provider_config(source)
    if missing:
        _print_provider_unconfigured(missing)
        return 2

    config = ProviderConfig.from_env(source)
    tool = ShoppingSearchTool(
        search_adapter=create_product_search_adapter(config),
        price_compare_adapter=create_price_compare_adapter(config),
    )
    result = tool.run(
        {
            "query": args.query,
            "budget_max": args.budget_max,
            "top_k": args.top_k,
        }
    )
    data = result.data or {}
    output = {
        "status": "success" if result.success else "failed",
        "provider": data.get("provider"),
        "capability": "shopping_search",
        "query": data.get("query") or args.query,
        "search_result": {
            "provider": (data.get("search") or {}).get("provider"),
            "item_count": len((data.get("search") or {}).get("items") or []),
            "items": (data.get("search") or {}).get("items") or [],
            "output_ref": (data.get("search") or {}).get("output_ref"),
        },
        "compare_result": {
            "offers": data.get("offers") or [],
            "best_offer": data.get("best_offer"),
            "ranking_reason": data.get("ranking_reason"),
            "output_ref": (data.get("comparison") or {}).get("output_ref")
            if isinstance(data.get("comparison"), dict)
            else None,
        },
        "output_ref": result.output_ref,
        "errors": data.get("errors") or [],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if result.success else 1


def _normalized_env(source: Mapping[str, str], local_json: str | None) -> dict[str, str]:
    normalized = dict(source)
    if local_json:
        normalized["PRODUCT_SEARCH_LOCAL_PATH"] = local_json
    if "PRODUCT_SEARCH_LOCAL_PATH" not in normalized and "PRODUCT_SEARCH_LOCAL_JSON" in normalized:
        normalized["PRODUCT_SEARCH_LOCAL_PATH"] = normalized["PRODUCT_SEARCH_LOCAL_JSON"]
    return normalized


def _missing_provider_config(source: Mapping[str, str]) -> str | None:
    product_provider = _product_provider(source)
    price_provider = _price_provider(source)
    if product_provider == "local_json" and not source.get("PRODUCT_SEARCH_LOCAL_PATH"):
        return "missing PRODUCT_SEARCH_LOCAL_PATH"
    if product_provider == "http":
        missing = []
        if not source.get("PRODUCT_SEARCH_BASE_URL"):
            missing.append("PRODUCT_SEARCH_BASE_URL")
        if not source.get("PRODUCT_SEARCH_API_KEY"):
            missing.append("PRODUCT_SEARCH_API_KEY")
        if missing:
            return f"missing {', '.join(missing)}"
    if product_provider == "haodanku" and not source.get("HAODANKU_API_KEY"):
        return "missing HAODANKU_API_KEY"
    if price_provider == "http":
        missing = []
        if not source.get("PRICE_COMPARE_BASE_URL"):
            missing.append("PRICE_COMPARE_BASE_URL")
        if not source.get("PRICE_COMPARE_API_KEY"):
            missing.append("PRICE_COMPARE_API_KEY")
        if missing:
            return f"missing {', '.join(missing)}"
    if price_provider == "haodanku" and not source.get("HAODANKU_API_KEY"):
        return "missing HAODANKU_API_KEY"
    if product_provider not in {"mock", "local_json", "http", "haodanku"}:
        return (
            "MULTIMODAL_AGENT_SHOPPING_PROVIDER must be mock, http, or haodanku; "
            "MULTIMODAL_AGENT_PRODUCT_PROVIDER may also use local_json."
        )
    if price_provider not in {"mock", "local", "http", "haodanku"}:
        return (
            "MULTIMODAL_AGENT_SHOPPING_PROVIDER must be mock, http, or haodanku; "
            "MULTIMODAL_AGENT_PRICE_PROVIDER may also use local."
        )
    return None


def _product_provider(source: Mapping[str, str]) -> str:
    return (
        source.get("MULTIMODAL_AGENT_PRODUCT_PROVIDER")
        or source.get("MULTIMODAL_AGENT_SHOPPING_PROVIDER")
        or "mock"
    )


def _price_provider(source: Mapping[str, str]) -> str:
    return (
        source.get("MULTIMODAL_AGENT_PRICE_PROVIDER")
        or source.get("MULTIMODAL_AGENT_SHOPPING_PROVIDER")
        or "mock"
    )


def _print_provider_unconfigured(reason: str) -> None:
    print("provider_unconfigured")
    print(reason)
    print("Please set MULTIMODAL_AGENT_SHOPPING_PROVIDER and the required shopping configuration.")


if __name__ == "__main__":
    raise SystemExit(main())
