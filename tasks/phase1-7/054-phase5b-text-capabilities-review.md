# Task 054 Phase 5B Review

## Goal

生成 Phase 5B 审计报告，确认 text-first capabilities 已完成。

## Read first

- `docs/53-phase5b-release-checklist.md`
- 当前 docs/
- 当前 tasks/
- 当前 src/
- 当前 tests/
- 当前 scripts/

## Requirements

生成：

```text
docs/54-phase5b-text-first-capabilities-review.md
```

报告包含：

1. Direct Chat 状态。
2. Image Generation 状态。
3. Prompt/output contract。
4. Mock 与真实 Provider 边界。
5. Smoke 能力。
6. Eval 覆盖。
7. 是否存在 key/data 泄露风险。
8. Phase 5C 建议。

允许小修：

- 更新 README 阶段状态。
- 补充文档链接。
- 更新 `.gitignore`。
- 删除缓存产物。

禁止：

- 接入商品搜索真实 Provider。
- 接入渲染真实 Provider。
- 写入 API Key。
- 提交真实生成图片。
- 大规模重构。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

## Stop condition

完成后停止，等待用户决定 Phase 5C。
