# Task 110 Demo Scenario Polish

## Goal

整理 demo scenarios，使 CLI 和 demo runner 都能稳定运行核心场景。

## Read first

- `docs/69-demo-scenario-matrix.md`
- `docs/116-phase6a-local-demo-entry-roadmap.md`
- `demo_data/scenarios/e2e_demo_scenarios.json`

## Requirements

- 至少 8 个 demo scenario 可运行。
- scenario 名称清晰。
- expected_tools 稳定。
- response_text 可读。
- 不依赖真实媒体。
- 不调用真实 Provider。

## Acceptance

```bash
python scripts/run_demo_flows.py
python -m pytest
```
