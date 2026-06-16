# Task 081 Phase 5G Video Understanding Roadmap

## Goal

确认 Phase 5G 只做轻量 video_understanding capability baseline，不做视频模型工程。

## Read first

- `docs/84-phase5g-video-understanding-roadmap.md`
- `docs/83-phase5f-hybrid-intent-router-review.md`
- 当前 README / docs index

## Scope

只更新文档和阶段说明，不做业务代码大改。

## Requirements

- 明确 Phase 5G 目标为 Video Understanding as External MLLM Capability。
- 明确 Agent 只负责意图识别、输入校验和工具调度。
- 明确外部 Video MLLM Provider 负责真实理解。
- 明确默认 mock。
- 明确不做自研视频模型、复杂抽帧、WebRTC、视频数据库。
- 不调用真实 Provider。
- 不写入 API Key。

## Suggested files

```text
docs/84-phase5g-video-understanding-roadmap.md
tasks/README_PHASE5G.md
README.md
```

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 082。
