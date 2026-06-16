# 85 Video Understanding Contract

## 目标

定义 `video_understanding` 的请求、结果、Adapter 和错误结构，让它成为 Assistant Agent 中可调度、可测试、可替换的 capability。

## 能力定义

`video_understanding` 用于：

```text
视频总结
视频问答
视频商品识别
视频场景识别
视频动作/事件识别
视频文字识别
为商品搜索、图片生成、3D 渲染提供视频上下文
```

## 推荐链路

```text
AgentGraphRuntime
  ↓
VideoUnderstandingTool
  ↓
VideoUnderstandingAdapter
  ↓
MockVideoUnderstandingAdapter / HttpVideoUnderstandingAdapter / External Video MLLM
```

## VideoUnderstandingRequest

建议字段：

```text
video_ref
user_query
user_id optional
session_id optional
max_frames optional
sample_strategy optional
metadata
memory_context optional
```

字段说明：

- `video_ref`：本地路径、对象存储引用、URL 或 mock id。
- `user_query`：用户希望从视频中获得什么。
- `max_frames`：仅供 Adapter 使用，Agent 不直接处理帧。
- `sample_strategy`：例如 uniform / keyframes / provider_default。
- `metadata`：可包含 duration、source_type、mime_type 等轻量信息。

## VideoUnderstandingResult

建议字段：

```text
summary
objects
actions
events
scene
products
brands
colors
materials
text_in_video
timestamps
style_tags
confidence optional
provider
model optional
output_ref
errors
latency_ms optional
```

示例：

```json
{
  "summary": "视频展示了一双白色低帮运动鞋在桌面上的商品展示。",
  "objects": ["white sneaker", "wooden table"],
  "actions": ["product display", "hand rotation"],
  "scene": "indoor product showcase",
  "products": ["white low-top sneaker"],
  "colors": ["white", "brown"],
  "materials": ["leather", "wood"],
  "text_in_video": [],
  "output_ref": "mock://video/understanding/demo"
}
```

## VideoUnderstandingAdapter

推荐接口：

```python
class VideoUnderstandingAdapter(Protocol):
    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        ...
```

## 默认实现

默认必须继续使用：

```text
MockVideoUnderstandingAdapter
```

Mock 输出应稳定、离线、可测试。

## HttpVideoUnderstandingAdapter Skeleton

可预留 HTTP Provider skeleton，但不能默认启用。

建议配置：

```text
MULTIMODAL_AGENT_VIDEO_PROVIDER=mock|http|qwen|openai_compatible|local
VIDEO_UNDERSTANDING_BASE_URL=
VIDEO_UNDERSTANDING_API_KEY=
VIDEO_UNDERSTANDING_MODEL=
VIDEO_UNDERSTANDING_TIMEOUT_SECONDS=
```

缺配置时返回：

```text
provider_unconfigured
```

## 错误码

```text
provider_unconfigured
provider_timeout
provider_bad_response
provider_auth_failed
provider_rate_limited
video_missing_input
video_file_too_large
video_unsupported_format
video_provider_unavailable
video_understanding_failed
```

## 输出安全

禁止：

- 默认上传真实视频。
- 在日志中输出 API Key。
- 在日志中输出完整视频 base64。
- 提交真实视频文件。
- 提交真实 Provider raw response。
- 自动下载视频 URL。
- 自动打开视频 URL。

## 验收标准

- `video_understanding` 有独立 request/result schema。
- `VideoUnderstandingAdapter` contract 明确。
- 默认 mock。
- 默认测试离线。
- Tool 不直接调用 HTTP / SDK。
