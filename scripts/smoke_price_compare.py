"""Manual smoke entry point for product_search -> price_compare capability."""

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
    PriceCompareInput,
    ProductSearchInput,
    create_price_compare_adapter,
    create_product_search_adapter,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a manual product_search to price_compare smoke test. Defaults use offline mock adapters.",
    )
    parser.add_argument("--query", required=True, help="Text query for product search and price compare.")
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
    search = create_product_search_adapter(config).search(
        ProductSearchInput(query=args.query, budget_max=args.budget_max, top_k=args.top_k)
    )
    if not search.success:
        output = {
            "status": "failed",
            "provider": search.provider,
            "capability": "price_compare",
            "query": args.query,
            "search_result": _search_payload(search),
            "compare_result": None,
            "errors": [error.model_dump(mode="json") for error in search.errors],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 1

    compare = create_price_compare_adapter(config).compare(
        PriceCompareInput(items=search.items, query=args.query, budget_max=args.budget_max, top_k=args.top_k)
    )
    output = {
        "status": "success" if compare.success else "failed",
        "provider": compare.provider,
        "capability": "price_compare",
        "query": args.query,
        "search_result": _search_payload(search),
        "compare_result": {
            "offers": [offer.model_dump(mode="json") for offer in compare.offers],
            "best_offer": compare.best_offer.model_dump(mode="json") if compare.best_offer else None,
            "ranking_reason": compare.ranking_reason.model_dump(mode="json") if compare.ranking_reason else None,
            "output_ref": compare.output_ref,
        },
        "errors": [error.model_dump(mode="json") for error in compare.errors],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if compare.success else 1


def _search_payload(search) -> dict:
    return {
        "provider": search.provider,
        "item_count": len(search.items),
        "items": [item.model_dump(mode="json") for item in search.items],
        "output_ref": search.output_ref,
    }


def _normalized_env(source: Mapping[str, str], local_json: str | None) -> dict[str, str]:
    normalized = dict(source)
    if local_json:
        normalized["PRODUCT_SEARCH_LOCAL_PATH"] = local_json
    if "PRODUCT_SEARCH_LOCAL_PATH" not in normalized and "PRODUCT_SEARCH_LOCAL_JSON" in normalized:
        normalized["PRODUCT_SEARCH_LOCAL_PATH"] = normalized["PRODUCT_SEARCH_LOCAL_JSON"]
    return normalized


def _missing_provider_config(source: Mapping[str, str]) -> str | None:
    product_provider = source.get("MULTIMODAL_AGENT_PRODUCT_PROVIDER", "mock")
    price_provider = source.get("MULTIMODAL_AGENT_PRICE_PROVIDER", "mock")
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
    if price_provider == "http":
        missing = []
        if not source.get("PRICE_COMPARE_BASE_URL"):
            missing.append("PRICE_COMPARE_BASE_URL")
        if not source.get("PRICE_COMPARE_API_KEY"):
            missing.append("PRICE_COMPARE_API_KEY")
        if missing:
            return f"missing {', '.join(missing)}"
    if product_provider not in {"mock", "local_json", "http"}:
        return "MULTIMODAL_AGENT_PRODUCT_PROVIDER must be mock, local_json, or http."
    if price_provider not in {"mock", "local", "http"}:
        return "MULTIMODAL_AGENT_PRICE_PROVIDER must be mock, local, or http."
    return None


def _print_provider_unconfigured(reason: str) -> None:
    print("provider_unconfigured")
    print(reason)
    print("Please set product/price provider variables for the selected smoke provider.")


if __name__ == "__main__":
    raise SystemExit(main())
