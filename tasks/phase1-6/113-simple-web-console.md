# Task 113 Simple Web Console

## Goal

新增一个最小 Web console 或静态 demo page。

## Requirements

- 提供文本输入框。
- 可选择 demo scenario。
- 可选填写 image_ref / video_ref。
- 展示 response_text。
- 展示 tool_calls。
- 展示 run_id / trace_id。
- 展示 errors。
- 不做登录。
- 不做复杂前端工程。
- 默认 mock/local。

## Suggested files

```text
src/multimodal_agent/api/static/
docs/demo-web-console.md
```

## Acceptance

```bash
python -m pytest
```
