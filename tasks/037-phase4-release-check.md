# Task 037 发布清理与 Phase 4 架构审计

## Goal

完成 Phase 4 后做发布检查和架构审计。

## Read first

- `docs/34-phase4-release-checklist.md`
- 当前 src/
- 当前 tests/
- 当前 docs/

## Requirements

生成：

```text
docs/35-phase4-architecture-review.md
```

报告包含：

1. 真实 Provider 接入状态。
2. 默认 Mock 与真实 Provider 边界。
3. Integration tests skip 策略。
4. WebSocket 事件流。
5. TaskQueue 抽象。
6. Memory 检索策略。
7. Failure Recovery 策略。
8. Graph Trace 能力。
9. API 协议版本。
10. Phase 5 建议。

允许小修：

- 更新 `.gitignore`。
- 删除缓存产物。
- 补充 README 阶段状态。
- 删除未使用 import。

禁止：

- 大规模重构。
- 接入新的真实 Provider。
- 修改仓库外文件。
- 删除大量源码。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

## Stop condition

完成后停止，等待用户决定 Phase 5。
