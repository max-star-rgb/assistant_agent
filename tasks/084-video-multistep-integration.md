# Task 084 Video Multistep Integration

## Goal

让 video_understanding 结果可以传给 product_search、price_compare、image_generation、render_3d、memory_save。

## Read first

- `docs/87-video-multistep-integration.md`
- 当前 planner
- 当前 capability validator
- 当前 tool_input_builder
- 当前 LangGraph loop
- 当前 response composer

## Requirements

支持以下链路：

```text
video_understanding
video_understanding → product_search
video_understanding → product_search → price_compare
video_understanding → image_generation
video_understanding → render_3d
video_understanding → memory_save
```

要求：

- 缺 video_ref 时进入 ask_followup。
- 有 video_ref 但文本是普通聊天时不强制 video_understanding。
- tool_input_builder 可把 video result 传给后续工具。
- response composer 能总结视频理解结果。
- 默认 mock-only。
- 不调用真实 Provider。

## Tests

新增或更新：

```text
tests/test_video_multistep_integration.py
tests/test_video_tool_input_builder.py
tests/test_video_capability_validator.py
```

覆盖：

- video summary。
- video → search。
- video → search → compare。
- video → generation。
- video → render。
- video → memory_save。
- missing video followup。
- video present but direct_chat.

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 085。
