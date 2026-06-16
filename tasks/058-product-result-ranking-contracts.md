# Task 058 Product Result and Ranking Contracts

## Goal

统一 ProductResult、PriceOffer、RankingReason 等 schema，并保证多步任务能复用这些结构。

## Read first

- `docs/58-product-result-and-ranking-contracts.md`
- 当前 product schemas
- 当前 tool results
- 当前 prompt builder / tool input builder

## Requirements

- ProductResult 字段稳定。
- PriceOffer 字段稳定。
- RankingReason 或 reason 字段可解释。
- product_search 输出可传给 price_compare。
- product_search 输出可传给 image_generation prompt_builder。
- product_search 输出可传给 render_3d。
- API 不暴露 provider raw response。

## Tests

新增或更新：

```text
tests/test_product_result_contracts.py
tests/test_product_multistep_data_flow.py
```

覆盖：

- search → compare。
- search → image_generation。
- search → render_3d。
- ranking reason。
- API output contract。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 059。
