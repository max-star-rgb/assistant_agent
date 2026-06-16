# Task 090 Provider Call Budget and Cost Guard

## Goal

增加 Provider 调用预算和成本保护，防止真实 Provider 未来被错误或循环调用。

## Read first

- `docs/94-provider-call-budget-and-cost-guard.md`
- 当前 runtime
- 当前 tool executor
- 当前 trace/run history
- 当前 LangGraph loop

## Requirements

- 定义 ProviderCallBudget 或等价结构。
- 记录每个 run 的 provider call count。
- 支持 max_provider_calls_per_run。
- 支持 per-capability max calls。
- 支持 estimated_cost 字段，允许 unknown。
- 预算超限返回结构化错误。
- Graph loop 或 tool executor 调用前检查 budget。
- 默认不调用真实 Provider。

## Tests

新增或更新：

```text
tests/test_provider_call_budget.py
tests/test_provider_budget_in_tool_executor.py
tests/test_budget_exceeded_response.py
```

覆盖：

- call count。
- max calls exceeded。
- per capability limit。
- budget error contract。
- response composer 可总结预算失败。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 091。
