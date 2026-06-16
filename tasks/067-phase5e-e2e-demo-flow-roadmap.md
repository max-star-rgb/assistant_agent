# Task 067 Phase 5E E2E Demo Flow Roadmap

## Goal

确认 Phase 5E 聚焦 End-to-End Demo Flow & Response Quality，不扩展新 Provider。

## Read first

- `docs/68-phase5e-e2e-demo-flow-roadmap.md`
- `docs/67-phase5d-render-capability-review.md`
- 当前 README / docs index

## Scope

只更新文档和阶段说明，不做业务代码大改。

## Requirements

- 明确 Phase 5E 不新增真实 Provider。
- 明确 Phase 5E 不做 Harness / MCP / Skills。
- 明确重点是 demo flow、output contract、response composer、eval layering、demo runner。
- 不调用真实 Provider。
- 不写入 API Key。

## Suggested files

```text
docs/68-phase5e-e2e-demo-flow-roadmap.md
tasks/README_PHASE5E.md
README.md
```

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 068。
