---
name: visual_understanding
description: Analyze attached images, camera frames, screenshots, or videos through governed vision and video tools.
enabled: true
disable-model-invocation: false
---
## Governed Tools
- vision_understanding
- video_understanding

## Permissions
- tool:vision_understanding
- tool:video_understanding

## Required Inputs
- vision_understanding: image_ids
- video_understanding: video_ref, video_ids

## When To Use
- User asks what is in an image, screenshot, camera frame, or attached visual.
- User asks to summarize, inspect, or reason about video content.
- User asks OCR-like questions about visible text, layout, objects, scene state, or product appearance.
- 用户询问图片、截图、摄像头画面、视频里有什么、发生了什么或可见文字。

## When Not To Use
- User asks to create or edit images; use image_creation instead.
- User asks to find products or compare shopping options after visual analysis; hand off to product_research when needed.
- User asks for current web information unrelated to supplied media.

## Safe Examples
- what is in this screenshot
- summarize this video
- 图片里这件商品是什么
- 读取画面上的文字

## Runtime Constraints
- Execute only through ToolExecutor; do not expose image bytes, video frames, provider raw responses, or media paths in final answers.
- Use video_understanding only when video references are present or explicitly supplied by the trusted entrypoint.
- Treat visual observations as current-run evidence, not durable memory.
- Follow existing media redaction and provider profile boundaries.
