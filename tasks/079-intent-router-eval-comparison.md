# Task 079 Intent Router Eval Comparison

## Goal

扩展 eval runner，支持比较 rule / mock_llm / hybrid router 的结果。

## Read first

- `docs/81-intent-router-eval-comparison.md`
- 当前 `scripts/run_evals.py`
- 当前 `tests/evals/eval_cases.json`
- 当前 router config

## Requirements

- Eval runner 支持 router mode 参数或等价机制。
- 默认仍为 rule。
- 支持 mock_llm。
- 支持 hybrid mock。
- 不调用真实 LLM。
- 增加模糊、多意图、缺输入 eval cases。
- 输出 router-level summary。
- 保留 failed_case_ids。

## Suggested commands

```bash
python scripts/run_evals.py --router rule
python scripts/run_evals.py --router mock_llm
python scripts/run_evals.py --router hybrid
```

如当前 CLI 不适合，可采用兼容方式。

## Tests

新增或更新：

```text
tests/test_intent_router_eval_comparison.py
```

覆盖：

- rule eval。
- mock_llm eval。
- hybrid eval。
- summary contains router mode。
- no real LLM call。

## Acceptance

```bash
python scripts/run_evals.py
python scripts/run_evals.py --router rule
python scripts/run_evals.py --router mock_llm
python scripts/run_evals.py --router hybrid
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 080。
