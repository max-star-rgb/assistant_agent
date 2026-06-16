# Task 115 Real Provider Opt-in Runbooks

## Goal

整理真实 Provider opt-in runbooks，但不默认调用真实 Provider。

## Read first

- `docs/118-phase6c-real-provider-opt-in-roadmap.md`
- 当前 smoke scripts
- `.env.example`

## Requirements

- 新增或整理 `docs/provider-setup.md`。
- 新增或整理 `docs/real-provider-smoke-runbook.md`。
- 覆盖 Vision / Chat / Image Generation / Product Search / Render / Video Provider。
- 每个 Provider 都说明 env vars、smoke command、缺配置行为。
- 不写 API Key。

## Acceptance

```bash
python -m pytest
```
