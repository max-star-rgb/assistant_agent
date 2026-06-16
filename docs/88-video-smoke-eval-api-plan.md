# 88 Video Smoke / Eval / API Plan

## 目标

为 `video_understanding` 增加轻量 smoke、eval 和 API 覆盖，确保它作为 Assistant capability 可用。

## Smoke 脚本

建议新增：

```text
scripts/smoke_video_understanding.py
```

默认 mock：

```bash
python scripts/smoke_video_understanding.py --video-ref mock://video/product-demo --text "总结这个视频"
```

真实 provider 手动 smoke：

```bash
export MULTIMODAL_AGENT_VIDEO_PROVIDER=http
export VIDEO_UNDERSTANDING_BASE_URL="<local-or-private-video-mllm-service>"
export VIDEO_UNDERSTANDING_API_KEY="<local-only>"
python scripts/smoke_video_understanding.py --video demo_data/videos/example.mp4 --text "总结这个视频"
```

## Smoke 安全要求

- import 脚本不触发 Provider。
- 默认 mock 可运行。
- 真实 provider 缺配置时清晰提示。
- 不写 API Key。
- 不提交真实视频。
- 不提交真实 Provider raw response。
- 不输出完整 base64。
- 不自动下载 video URL。
- 不自动打开 video URL。

## Eval 覆盖

建议新增或扩展 eval cases：

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

示例：

```json
{
  "id": "video_summary_001",
  "user_query": "总结这个视频里的商品和场景",
  "inputs": {"has_video": true},
  "expected_intent": "video_understanding",
  "expected_tools": ["video_understanding"]
}
```

```json
{
  "id": "video_search_compare_001",
  "user_query": "找视频里的商品并比较价格",
  "inputs": {"has_video": true},
  "expected_intent": "multi_step_orchestration",
  "expected_tools": ["video_understanding", "product_search", "price_compare"]
}
```

## API 覆盖

HTTP API 应能返回：

```text
intent
tool_calls
tool_results
contract
video understanding result
errors
```

WebSocket 可继续沿用 event sink：

```text
tool_started: video_understanding
tool_finished: video_understanding
final_response
```

## Demo Runner 覆盖

E2E demo runner 应支持：

```text
video_understanding
video_to_product_search
video_to_render
```

默认使用 mock video_ref。

## 验收标准

- video smoke 默认 mock 可运行。
- eval 默认离线。
- API 返回 video contract。
- WebSocket 可观察 video tool event。
- demo runner 有视频场景。
- 不调用真实 Video Provider。
