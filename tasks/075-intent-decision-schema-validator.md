# Task 075 IntentDecision Schema and Capability Validator

## Goal

新增统一 IntentDecision schema，并实现 CapabilityValidator。

## Read first

- `docs/77-intent-decision-schema-and-validator.md`
- 当前 intent/router/planner schema
- 当前 UserRequest schema
- 当前 capability contracts

## Requirements

- 定义 IntentDecision schema。
- 定义 PlanStep schema 或复用现有 PlanStep 并补齐字段。
- 定义 CapabilityValidator。
- Validator 检查 image/video/render/price/memory 等必要输入。
- Validator 缺输入时输出 ask_followup。
- Validator 不调用工具。
- Validator 不调用真实 Provider。
- 保持现有 tests 兼容。

## Tests

新增或更新：

```text
tests/test_intent_decision_schema.py
tests/test_capability_validator.py
```

覆盖：

- valid direct_chat。
- image_understanding without image → ask_followup。
- video_understanding without video → ask_followup。
- render_3d without scene → ask_followup。
- price_compare without products but with query → product_search + price_compare。
- memory_retrieval without session/user context → ask_followup or structured missing input。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 076。
