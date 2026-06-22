# Task 074 Phase 5F Hybrid Intent Router Roadmap

## Goal

确认 Phase 5F 聚焦 Hybrid Intent Router & Planner Quality，不扩展新 Provider，不实现 MCP / Skills。

## Read first

- `docs/76-phase5f-hybrid-intent-router-roadmap.md`
- `docs/75-phase5e-e2e-demo-flow-review.md`
- 当前 README / docs index

## Scope

只更新文档和阶段说明，不做业务代码大改。

## Requirements

- 明确 Phase 5F 目标为 intent routing 和 planner quality。
- 明确规则路由继续作为默认。
- 明确 LLM Router 只能 optional / mockable / default-off。
- 明确 LLM 不直接执行工具。
- 明确 Validator 是执行前安全闸门。
- 不调用真实 LLM。
- 不调用真实 Provider。
- 不写入 API Key。

## Suggested files

```text
docs/76-phase5f-hybrid-intent-router-roadmap.md
tasks/README_PHASE5F.md
README.md
```

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 075。
