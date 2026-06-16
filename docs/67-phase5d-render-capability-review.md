# 67 Phase 5D Render Capability Review

## 1. Render Capability 状态

Phase 5D 已完成轻量 `render_3d` capability baseline。

`render_3d` 当前是 Intent-driven Assistant Agent 的一个可调度能力，不是独立 3D 渲染平台。它已接入：

- 意图识别：文本中的“渲染 / 3D / 放到 / 客厅 / 展厅 / 展示”等表达可触发 `render_3d`。
- 多步规划：`RuleBasedTaskPlanner` 可把 `render_3d` 作为普通 plan step。
- Tool Registry：默认通过 `create_render_adapter()` 注入 `Render3DTool`。
- LangGraph Runtime：Graph loop 通过通用 tool execution 执行 `render_3d`，没有平台级特殊分支。
- API / WebSocket：HTTP `/agent/run` 与 WebSocket event stream 都能观察 render tool call / result。

默认运行路径仍是离线 mock，不调用真实 Render Provider。

## 2. RenderRequest / RenderResult Contract

`RenderRequest` 定义在 `src/multimodal_agent/services/render_adapter.py`，用于描述轻量场景渲染请求。核心字段包括：

- `scene_description`
- `product_ref`
- `product_title`
- `product_image_url`
- `model_ref`
- `image_ref`
- `video_ref`
- `visual_summary`
- `video_summary`
- `style`
- `camera_angle`
- `lighting`
- `output_format`
- `width`
- `height`
- `user_id`
- `session_id`
- `memory_context`

为兼容早期任务，当前仍保留：

- `RenderInput = RenderRequest`
- `product_id`
- `image_url`
- `scene`
- `material`
- `camera`
- `create_render()` 兼容 alias

`RenderResult` 定义在 `src/multimodal_agent/schemas/generation.py`，核心字段包括：

- `task_id`
- `render_id`
- `status`
- `provider`
- `output_ref`
- `preview_url`
- `image_url`
- `video_url`
- `model_url`
- `scene_description`
- `used_inputs`
- `errors`
- `latency_ms`
- `error`

当前 mock 成功输出稳定为：

```text
mock://render/preview.png
mock://render/model.glb
```

缺少场景描述时返回结构化错误：

```text
render_missing_scene
```

## 3. Mock / HTTP Provider 边界

默认 provider 是 `mock`：

- `ProviderConfig.render_provider == "mock"`
- `create_render_adapter()` 默认返回 `MockRenderAdapter`
- `MockRenderAdapter` 不读网络、不读真实模型、不生成真实文件

可选 `http` provider 仅是 skeleton：

- `MULTIMODAL_AGENT_RENDER_PROVIDER=http`
- `RENDER_BASE_URL`
- `RENDER_API_KEY`
- `RENDER_TIMEOUT_SECONDS`

如果选择 `http` 但缺少配置，返回：

```text
provider_unconfigured
```

即使配置齐全，当前 `HttpRenderAdapter` 也不会发真实 HTTP 请求，而是返回：

```text
render_provider_unavailable
```

这符合 Phase 5D 边界：只完成可选接入结构，不接真实 Blender / Unity / Three.js / 渲染服务。

## 4. 多步链路状态

`build_render_request_input()` 已支持把上游 tool result 转为 `RenderRequest` payload：

| 链路 | 状态 | 关键字段 |
| --- | --- | --- |
| text -> render_3d | 已支持 | `scene_description`, `scene`, `user_id`, `session_id` |
| product_search -> render_3d | 已支持 | `product_ref`, `product_title`, `product_image_url`, `image_url`, `style` |
| image_understanding -> render_3d | 已支持 | `visual_summary`, `image_ref`, `style`, `material` |
| video_understanding -> render_3d | 已支持 | `video_summary`, `video_ref`, `image_ref` |
| memory_retrieval -> render_3d | 已支持 | `product_ref`, `style`, `memory_context` |

对应覆盖：

- `tests/test_render_tool_input_builder.py`
- `tests/test_render_multistep_integration.py`
- `tests/test_assistant_multistep_orchestration.py`

## 5. Smoke 能力

新增脚本：

```text
scripts/smoke_render_3d.py
```

默认 mock 示例：

```bash
python scripts/smoke_render_3d.py --scene "北欧风客厅" --product "浅灰色布艺沙发"
```

安全边界：

- import 脚本不会触发 provider。
- 默认 mock smoke 可运行。
- http provider 缺配置时以 `provider_unconfigured` 清晰退出。
- 输出 JSON，不输出 API Key、Authorization header、Bearer token、真实模型路径或真实渲染产物。

对应覆盖：

- `tests/test_render_smoke_script.py`

## 6. Eval / API 覆盖

Eval 已加入 5 个 render cases：

- `text_only_render`
- `product_search_to_render`
- `image_understanding_to_render`
- `video_understanding_to_render`
- `memory_to_render`

默认 eval 使用 `AgentWorkflow` 与 mock/local provider，不调用真实 Render Provider。

API 覆盖：

- `tests/test_render_api.py`
- `tests/test_websocket_graph_runtime.py`

HTTP `/agent/run` 可返回：

- `intent`
- `tool_calls`
- `tool_results`
- `output_ref`
- render result data
- `errors`

WebSocket 可观察：

- `tool_started`
- `tool_finished`
- `final_response`

## 7. 安全边界

Phase 5D 保持以下边界：

- 不写入 API Key。
- 不创建包含真实密钥的 `.env` 或 `.env.local`。
- 不提交真实 3D 模型、渲染图片或渲染视频。
- 不接入真实 Blender / Unity / Three.js。
- 不做复杂材质系统。
- 不做模型资产管理平台。
- 不做渲染农场。
- 不做生产级任务队列。
- 默认 pytest 不调用真实 Render Provider。
- 默认 eval 不调用真实 Render Provider。
- 真实 Render Provider 只能由用户显式配置并手动运行 smoke 或未来 env-gated integration tests。

渲染产物目录应保持在 `.local/` 或 `.local/rendered/` 下，并由 `.gitignore` 忽略。

## 8. Phase 5E 建议

Phase 5D 完成后，Assistant Agent 已具备以下 capability baseline：

- `direct_chat`
- `image_generation`
- `image_understanding`
- `video_understanding`
- `product_search`
- `price_compare`
- `render_3d`
- `memory_retrieval`
- `multi_step_orchestration`

建议 Phase 5E 不进入独立业务平台建设，而聚焦 Agent 质量：

1. Capability output contract 统一化：为各 capability 建立一致的 `contract` 字段与错误结构。
2. Intent routing 质量提升：在不默认调用真实 LLM 的前提下，引入可替换的 intent classifier adapter。
3. 多步结果融合：让 response composer 更好地总结多步 tool result，而不是只给出通用完成语。
4. Eval suite 分层：区分 routing eval、tool contract eval、API contract eval 和 smoke eval。
5. Provider safety hardening：统一敏感信息脱敏、base64 截断、provider error mapping。
6. Memory relevance 改进：让多轮偏好、商品和生成/渲染上下文更稳定地参与 planning。

Phase 5E 仍应保持默认 mock/local-first，真实外部 Provider 继续 opt-in。
