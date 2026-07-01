"""Manual smoke entry point for product_search capability."""

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
from assistant_agent.services.product_adapter import ProductSearchInput, create_product_search_adapter


PRODUCT_PROVIDER_REQUIREMENTS = {
    "local_json": "PRODUCT_SEARCH_LOCAL_PATH",
    "http": "PRODUCT_SEARCH_BASE_URL and PRODUCT_SEARCH_API_KEY",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a manual product_search smoke test. Defaults use the offline mock adapter.",
    )
    parser.add_argument("--query", required=True, help="Text query for product search.")
    parser.add_argument("--budget-max", type=float, default=None, help="Optional maximum product price.")
    parser.add_argument("--top-k", type=int, default=5, help="Maximum number of products to return.")
    parser.add_argument("--local-json", default=None, help="Small local JSON product dataset for local_json provider.")
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = _normalized_env(os.environ if env is None else env, args.local_json)
    provider = source.get("MULTIMODAL_AGENT_PRODUCT_PROVIDER", "mock")

    missing = _missing_provider_config(provider, source)
    if missing:
        _print_provider_unconfigured(missing)
        return 2

    adapter = create_product_search_adapter(ProviderConfig.from_env(source))
    result = adapter.search(
        ProductSearchInput(query=args.query, budget_max=args.budget_max, top_k=args.top_k)
    )
    output = {
        "status": "success" if result.success else "failed",
        "provider": result.provider,
        "capability": "product_search",
        "query": args.query,
        "item_count": len(result.items),
        "items": [item.model_dump(mode="json") for item in result.items],
        "output_ref": result.output_ref,
        "errors": [error.model_dump(mode="json") for error in result.errors],
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


def _missing_provider_config(provider: str, source: Mapping[str, str]) -> str | None:
    if provider == "local_json" and not source.get("PRODUCT_SEARCH_LOCAL_PATH"):
        return "missing PRODUCT_SEARCH_LOCAL_PATH"
    if provider == "http":
        missing = []
        if not source.get("PRODUCT_SEARCH_BASE_URL"):
            missing.append("PRODUCT_SEARCH_BASE_URL")
        if not source.get("PRODUCT_SEARCH_API_KEY"):
            missing.append("PRODUCT_SEARCH_API_KEY")
        if missing:
            return f"missing {', '.join(missing)}"
    if provider not in {"mock", *PRODUCT_PROVIDER_REQUIREMENTS}:
        return "MULTIMODAL_AGENT_PRODUCT_PROVIDER must be mock, local_json, or http."
    return None


def _print_provider_unconfigured(reason: str) -> None:
    print("provider_unconfigured")
    print(reason)
    print("Please set MULTIMODAL_AGENT_PRODUCT_PROVIDER and the required local product configuration.")


if __name__ == "__main__":
    raise SystemExit(main())
