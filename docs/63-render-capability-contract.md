# 63 Render Capability Contract

## 目标

定义 `render_3d` 的最小输入、输出、Adapter 和错误结构，让它成为 Assistant Agent 中可调度、可测试、可替换的 capability。

## 能力定义

`render_3d` 用于：

- 根据文本描述生成 3D / 场景渲染预览。
- 将商品放入指定场景。
- 基于图片理解结果构造渲染任务。
- 基于视频理解结果构造渲染任务。
- 基于商品搜索结果生成展示图、预览图或模型展示任务。

## 推荐链路

```text
AgentGraphRuntime
  ↓
Render3DTool
  ↓
RenderAdapter
  ↓
MockRenderAdapter / HttpRenderAdapter
```

## RenderRequest

建议字段：

```text
scene_description
product_ref optional
product_title optional
product_image_url optional
model_ref optional
image_ref optional
video_ref optional
visual_summary optional
video_summary optional
style optional
camera_angle optional
lighting optional
output_format optional
width optional
height optional
user_id
session_id
memory_context optional
```

## RenderResult

建议字段：

```text
render_id
provider
status
output_ref
preview_url optional
image_url optional
video_url optional
model_url optional
scene_description
used_inputs
errors
latency_ms optional
```

## RenderAdapter

推荐接口：

```python
class RenderAdapter(Protocol):
    def render(self, request: RenderRequest) -> RenderResult:
        ...
```

## 默认实现

默认必须继续使用：

```text
MockRenderAdapter
```

Mock 输出示例：

```text
mock://render/preview.png
mock://render/model.glb
```

## HttpRenderAdapter Skeleton

可以预留 HTTP Provider skeleton，但不能默认启用。

建议配置：

```text
MULTIMODAL_AGENT_RENDER_PROVIDER=mock|http
RENDER_BASE_URL=
RENDER_API_KEY=
RENDER_TIMEOUT_SECONDS=
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
render_missing_scene
render_missing_asset
render_task_failed
render_provider_unavailable
```

## 输出安全

禁止：

- 自动打开 URL。
- 自动下载模型。
- 自动提交真实 3D 文件。
- 在日志中输出 API Key。
- 在测试中调用真实渲染服务。
- 把大型渲染产物提交到仓库。

建议真实/本地渲染输出目录：

```text
.local/rendered/
```

该目录必须被 `.gitignore` 忽略。

## 验收标准

- `render_3d` 支持纯文本场景描述。
- `render_3d` 可接 ProductResult。
- `render_3d` 可接 visual_summary / video_summary。
- 默认 mock。
- 默认测试离线。
- Tool 不直接调用 HTTP / SDK。
