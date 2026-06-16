# Task 086 Phase 5G Review

## Goal

生成 Phase 5G 审计报告，确认 video_understanding capability baseline 已完成。

## Read first

- `docs/89-phase5g-video-understanding-review-checklist.md`
- 当前 docs/
- 当前 tasks/
- 当前 src/
- 当前 tests/
- 当前 scripts/
- 当前 demo_data/

## Requirements

生成：

```text
docs/90-phase5g-video-understanding-review.md
```

报告包含：

1. Video Understanding Capability 状态。
2. Request / Result / Adapter contract。
3. Mock / HTTP / external Provider 边界。
4. 多步链路状态。
5. Smoke 能力。
6. Eval / API / WebSocket / Demo 覆盖。
7. 安全边界。
8. Phase 5H 建议。

允许小修：

- 更新 README 阶段状态。
- 补充文档链接。
- 更新 `.gitignore`。
- 删除缓存产物。

禁止：

- 接入真实 Video Provider。
- 上传真实视频。
- 做复杂抽帧系统。
- 做 WebRTC。
- 做视频数据库。
- 写入 API Key。
- 大规模重构。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
git status --short
```

## Stop condition

完成后停止，等待用户决定 Phase 5H。
