# Task 047 Phase 5A Assistant Routing Review

## Goal

生成 Phase 5A 审计报告，确认 Assistant Capability Routing Baseline 已完成。

## Read first

- `docs/46-phase5a-assistant-routing-review-checklist.md`
- 当前 docs/
- 当前 tasks/
- 当前 src/
- 当前 tests/evals/

## Requirements

生成：

```text
docs/47-phase5a-assistant-routing-review.md
```

报告包含：

1. Assistant Agent 定位。
2. Capability matrix。
3. Text-only 能力状态。
4. Media-aware routing 状态。
5. Multi-step routing 状态。
6. Eval 覆盖情况。
7. Vision Provider validation 的降级定位。
8. 仍然是 Mock 的能力。
9. 下一阶段建议。

允许小修：

- 更新 README 阶段状态。
- 补充文档链接。
- 清理旧 Vision-only Phase 5A 的误导性引用。
- 不删除历史文件，必要时标记为 archived / provider validation。

禁止：

- 接入新 Provider。
- 写入 API Key。
- 提交真实图片或视频。
- 大规模重构。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

## Stop condition

完成后停止，等待用户决定下一阶段。
