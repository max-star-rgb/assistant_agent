# Task 056 Product Search Provider Adapter

## Goal

为 product_search 定义明确的 Provider Adapter contract，并支持 mock/local_json/http skeleton。

## Read first

- `docs/56-product-search-provider-design.md`
- 当前 product search tool / adapter
- 当前 ProviderConfig
- 当前 ToolRegistry

## Requirements

- 检查或定义 ProductSearchAdapter Protocol。
- 检查或定义 ProductSearchRequest / ProductSearchResult / ProductResult schema。
- 默认使用 MockProductSearchAdapter。
- 可新增 LocalJsonProductSearchAdapter。
- 可新增 HttpProductSearchAdapter skeleton，但不默认启用。
- 缺配置返回 provider_unconfigured。
- Tool 不直接调用 HTTP / SDK。
- 不联网测试。

## Tests

新增或更新：

```text
tests/test_product_search_adapter.py
tests/test_product_search_provider_selection.py
```

覆盖：

- mock default。
- text-only query。
- visual_summary query。
- local_json provider。
- http provider 缺配置。
- 不直接调用真实 Provider。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 057。
