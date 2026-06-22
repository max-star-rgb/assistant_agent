# Task 059 Product Search / Price Compare Smoke Scripts

## Goal

为 product_search 和 price_compare 提供手动 smoke 脚本，默认 mock/local，不联网。

## Read first

- `docs/59-product-search-smoke-and-safety.md`
- 当前 scripts/
- 当前 demo_data/
- 当前 adapters

## Requirements

新增或更新：

```text
scripts/smoke_product_search.py
scripts/smoke_price_compare.py
demo_data/products/products.example.json
```

要求：

- import 脚本不触发 Provider。
- 默认 mock smoke 可运行。
- local_json smoke 可运行。
- http provider 缺配置时清晰提示。
- 不写 API Key。
- 不爬虫。
- 不下单。
- 不提交大规模真实商品数据。

## Tests

新增或更新：

```text
tests/test_product_search_smoke_scripts.py
tests/test_price_compare_smoke_scripts.py
```

覆盖：

- import safe。
- default mock。
- local_json。
- missing config。
- output JSON structure。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 060。
