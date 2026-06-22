# Task 028 仓库清理与 Phase 3 发布检查

## Goal

完成 Phase 3 后做一次发布前检查和架构审计。

## Read first

- `docs/24-phase3-release-checklist.md`
- `docs/16-phase2-architecture-review.md`
- 当前 src/
- 当前 tests/

## Scope

生成 Phase 3 审计报告，并做小范围清理。

## Requirements

生成：

```text
docs/25-phase3-architecture-review.md
```

报告包含：

1. 默认 runtime 入口。
2. LangGraph 文件和节点。
3. Node 边界是否干净。
4. Memory backend 策略。
5. Provider contract tests。
6. Integration tests skip 策略。
7. Eval 指标。
8. 是否仍有 Mock 能力。
9. Phase 4 建议。

允许小修：删除未使用 import、补充文档链接、更新 README 中 Phase 3 状态、更新 `.gitignore` 避免缓存和构建产物污染。

禁止：大规模重构、新增真实 Provider、删除大量代码、修改仓库外文件。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

## Stop condition

完成后停止，等待用户决定 Phase 4。
