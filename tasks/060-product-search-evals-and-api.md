# Task 060 Product Search Evals and API Coverage

## Goal

增强 product_search / price_compare 的 eval 和 API 覆盖。

## Read first

- 当前 `tests/evals/eval_cases.json`
- 当前 `scripts/run_evals.py`
- 当前 API routes
- `docs/60-phase5c-release-checklist.md`

## Requirements

- 增加 text-only product_search eval cases。
- 增加 text-only price_compare eval cases。
- 增加 image/video summary → product_search cases。
- 增加 product_search → price_compare multistep cases。
- API response 能稳定返回 product_search / price_compare contract。
- 默认 eval 不联网。
- 默认 eval 不调用真实 Provider。

## Tests

新增或更新：

```text
tests/test_product_search_api.py
tests/test_price_compare_api.py
tests/test_product_search_evals.py
```

## Acceptance

```bash
python scripts/run_evals.py
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 061。
