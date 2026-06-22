# Task 076 Rule Router Confidence Refactor

## Goal

让 Rule Router 输出 IntentDecision、confidence、matched_rules 和 reason。

## Read first

- `docs/78-rule-router-confidence-refactor.md`
- 当前 `src/multimodal_agent/agent/intent.py`
- 当前 `src/multimodal_agent/agent/router.py`
- 当前 eval cases

## Requirements

- Rule Router 输出 IntentDecision。
- 每个结果包含 confidence。
- 每个结果包含 source=rule。
- 命中的规则记录在 matched_rules。
- reason 可用于 trace/debug。
- 多规则命中可生成 multi_step_orchestration plan_steps。
- 高置信规则继续直接通过。
- 低置信规则为后续 hybrid fallback 做准备。
- 输出必须经过 CapabilityValidator。

## Tests

新增或更新：

```text
tests/test_rule_router_confidence.py
tests/test_rule_router_intent_decision.py
```

覆盖：

- high confidence image_generation。
- high confidence direct_chat。
- multi-step product_search + price_compare。
- ambiguous low confidence。
- matched_rules。
- validator integration。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 077。
