# 65 Render Smoke / Eval / API Plan

## 目标

为 `render_3d` 提供轻量 smoke、eval 和 API 覆盖，确保它作为 Assistant capability 可用。

## Smoke 脚本

建议新增：

```text
scripts/smoke_render_3d.py
```

默认 mock：

```bash
python scripts/smoke_render_3d.py --scene "北欧风客厅" --product "浅灰色布艺沙发"
```

可选 HTTP Provider：

```bash
export MULTIMODAL_AGENT_RENDER_PROVIDER=http
export RENDER_BASE_URL="<local-or-private-render-service>"
export RENDER_API_KEY="<local-only>"
python scripts/smoke_render_3d.py --scene "现代办公室" --product "黑色办公椅"
```

## Smoke 安全要求

- import 脚本不触发 Provider。
- 默认 mock 可运行。
- 缺真实 Provider 配置时清晰提示。
- 不写 API Key。
- 不提交真实渲染结果。
- 不自动下载模型。
- 不自动打开 URL。
- 输出 JSON，不输出敏感 header。

## Eval 覆盖

建议新增或扩展 eval cases：

```text
text_only_render
product_search_to_render
image_understanding_to_render
video_understanding_to_render
memory_to_render
```

示例：

```json
{
  "id": "text_render_001",
  "user_query": "把一把浅灰色沙发放到北欧风客厅里看看",
  "inputs": {"has_image": false, "has_video": false},
  "expected_intent": "render_3d",
  "expected_tools": ["render_3d"]
}
```

```json
{
  "id": "search_render_001",
  "user_query": "帮我找一款黑色办公椅，然后放到现代办公室里看看",
  "inputs": {"has_image": false, "has_video": false},
  "expected_intent": "multi_step_orchestration",
  "expected_tools": ["product_search", "render_3d"]
}
```

## API 覆盖

HTTP API 应能返回：

```text
intent
tool_calls
tool_results
contract
output_ref
render result data
errors
```

WebSocket 可继续沿用 runtime event sink：

```text
tool_started: render_3d
tool_finished: render_3d
final_response
```

## 验收标准

- render smoke 默认 mock 可运行。
- render eval 默认离线。
- API 可返回 render contract。
- WebSocket 可观察 render tool event。
- 不调用真实渲染服务。
