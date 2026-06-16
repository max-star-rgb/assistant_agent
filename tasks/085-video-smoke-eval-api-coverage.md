# Task 085 Video Smoke / Eval / API Coverage

## Goal

为 video_understanding 增加 smoke、eval、API、WebSocket 和 demo runner 覆盖。

## Read first

- `docs/88-video-smoke-eval-api-plan.md`
- 当前 scripts/
- 当前 tests/evals/
- 当前 API routes
- 当前 WebSocket tests
- 当前 demo runner

## Requirements

新增或更新：

```text
scripts/smoke_video_understanding.py
tests/test_video_smoke_script.py
tests/test_video_api.py
tests/test_video_evals.py
tests/test_video_demo_runner.py
```

要求：

- smoke 脚本 import 不触发 Provider。
- 默认 mock smoke 可运行。
- 缺真实 Provider 配置时清晰提示。
- eval 默认离线。
- API 返回 video contract。
- WebSocket 可观察 video_understanding event。
- demo runner 有 video scenarios。
- 不提交真实视频。
- 不调用真实 Provider。

## Eval cases

至少覆盖：

```text
video_understanding
video_to_product_search
video_to_price_compare
video_to_image_generation
video_to_render
video_to_memory_save
video_missing_input_followup
video_present_but_text_chat
```

## Acceptance

```bash
python scripts/run_evals.py
python scripts/run_demo_flows.py
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 086。
