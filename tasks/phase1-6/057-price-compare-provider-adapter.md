# Task 057 Price Compare Provider Adapter

## Goal

为 price_compare 定义明确的 adapter / compare contract，并支持结构化比价输出。

## Read first

- `docs/57-price-compare-provider-design.md`
- 当前 price compare tool
- 当前 product search adapter
- 当前 schemas

## Requirements

- 检查或定义 PriceCompareAdapter Protocol，或明确兼容 ProductSearchAdapter.compare()。
- 检查或定义 PriceCompareRequest / PriceCompareResult / PriceOffer schema。
- 没有候选商品时返回结构化错误。
- 有 ProductResult 时可生成 offers。
- 默认 mock/local。
- 不联网。
- 不处理购买/下单/支付。

## Tests

新增或更新：

```text
tests/test_price_compare_adapter.py
tests/test_price_compare_tool.py
```

覆盖：

- no products error。
- products → offers。
- budget filtering。
- best_offer selection。
- error code 稳定。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 058。
