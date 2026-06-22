# Task 073 Phase 5E Review

## Goal

生成 Phase 5E 审计报告，确认 End-to-End Demo Flow & Response Quality 已完成。

## Read first

- `docs/74-phase5e-review-checklist.md`
- 当前 docs/
- 当前 tasks/
- 当前 src/
- 当前 tests/
- 当前 scripts/
- 当前 demo_data/

## Requirements

生成：

```text
docs/75-phase5e-e2e-demo-flow-review.md
```

报告包含：

1. Demo scenario matrix 状态。
2. Capability output contract 状态。
3. Response composer 改进状态。
4. Eval suite 分层状态。
5. E2E demo runner 状态。
6. 默认 mock/local 安全边界。
7. 仍然是 Mock 的能力。
8. Phase 5F 建议。

允许小修：

- 更新 README 阶段状态。
- 补充文档链接。
- 更新 `.gitignore`。
- 删除缓存产物。

禁止：

- 新增真实 Provider。
- 写入 API Key。
- 提交真实图片/视频/生成图/渲染产物。
- 大规模重构。
- MCP / Skills 打包。
- Hybrid LLM Intent Router 实现。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
git status --short
```

## Stop condition

完成后停止，等待用户决定 Phase 5F。
