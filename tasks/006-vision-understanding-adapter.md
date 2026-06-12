# Task 006：图片/视频理解适配器

## Goal

实现视觉理解 adapter 接口和 mock 实现，为 VLM / Video MLLM 接入预留位置。

## Read first

- `docs/01-architecture.md`
- `docs/05-tool-contracts.md`

## Scope

新增/修改：

```text
src/multimodal_agent/services/vision_adapter.py
src/multimodal_agent/tools/vision_tool.py
tests/unit/test_vision_adapter.py
```

## Steps

1. 定义 `VisionUnderstandingAdapter` 接口。
2. 实现 `MockVisionUnderstandingAdapter`。
3. 输入包含 image_ids、video_ids、question。
4. 输出 `VisualUnderstandingResult`。
5. 在 `VisionUnderstandingTool` 中调用 adapter。

## Acceptance

```bash
pytest tests/unit/test_vision_adapter.py
```

mock 对 video_id 返回：白色低帮运动鞋、简约日系风格、室内展示场景。

## Out of scope

- 不接入真实 VLM。
- 不实现 FFmpeg 抽帧。
