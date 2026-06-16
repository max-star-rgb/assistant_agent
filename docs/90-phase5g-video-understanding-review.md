# 90 Phase 5G Video Understanding Review

## 结论

Phase 5G Video Understanding as External MLLM Capability 已完成 baseline。

当前系统已经把 `video_understanding` 作为独立 Assistant capability 接入默认运行链路。Agent 负责意图识别、输入校验、工具调度和结果传递；真实视频理解仍被定义为外部 Video MLLM / VLM Provider 的职责。默认运行、默认测试、默认 eval 和默认 demo runner 继续使用 `MockVideoUnderstandingAdapter`，不会调用真实 Video Provider。

本阶段没有自研视频模型，没有实现复杂抽帧系统，没有实现 WebRTC，没有建设视频数据库，也没有写入 API Key 或提交真实视频/视频帧。

## 1. Capability 状态

`video_understanding` 已从早期 `vision_understanding` 视频旁路中拆出，成为独立 tool/capability：

```text
src/multimodal_agent/tools/video_tool.py
src/multimodal_agent/services/video_adapter.py
src/multimodal_agent/schemas/perception.py
```

能力契约已更新：

```text
video_understanding -> VideoUnderstandingResult -> video_understanding tool
```

关键边界：

- 图片理解仍使用 `vision_understanding`。
- 视频理解默认使用 `video_understanding`。
- `VideoUnderstandingTool` 不直接调用 HTTP / SDK。
- 默认 adapter 是 `MockVideoUnderstandingAdapter`。

## 2. Request / Result / Adapter Contract

已定义：

```text
VideoUnderstandingRequest
VideoUnderstandingResult
VideoUnderstandingAdapter
MockVideoUnderstandingAdapter
HttpVideoUnderstandingAdapter
```

`VideoUnderstandingRequest` 支持 `video_ref`、`user_query`、`user_id`、`session_id`、`max_frames`、`sample_strategy`、`metadata` 和 `memory_context`。

`VideoUnderstandingResult` 支持 `summary`、`objects`、`actions`、`events`、`scene`、`products`、`brands`、`colors`、`materials`、`text_in_video`、`timestamps`、`style_tags`、`confidence`、`provider`、`model`、`output_ref`、`errors` 和 `latency_ms`。

适配器 contract：

```python
def understand_video(request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
    ...
```

## 3. Provider 边界

默认 provider：

```text
MULTIMODAL_AGENT_VIDEO_PROVIDER=mock
```

可选 skeleton：

```text
MULTIMODAL_AGENT_VIDEO_PROVIDER=http
VIDEO_UNDERSTANDING_BASE_URL=
VIDEO_UNDERSTANDING_API_KEY=
VIDEO_UNDERSTANDING_MODEL=
VIDEO_UNDERSTANDING_TIMEOUT_SECONDS=
MULTIMODAL_AGENT_MAX_VIDEO_BYTES=
MULTIMODAL_AGENT_MAX_VIDEO_SECONDS=
```

`HttpVideoUnderstandingAdapter` 当前只做配置和安全校验：

- 缺 `VIDEO_UNDERSTANDING_BASE_URL` 返回 `provider_unconfigured`。
- 缺 `VIDEO_UNDERSTANDING_API_KEY` 返回 `provider_unconfigured`。
- 超过大小/时长限制返回 `video_file_too_large`。
- 即使配置完整，也只返回 `video_provider_unavailable` skeleton 状态，不做网络 I/O。

真实 Provider 只能由后续阶段通过明确任务接入。

## 4. 多步链路状态

已支持：

```text
video_understanding
video_understanding -> product_search
video_understanding -> product_search -> price_compare
video_understanding -> image_generation
video_understanding -> render_3d
video_understanding -> memory_save
```

输入传递已覆盖：

- `video_summary` 传给商品搜索和渲染。
- `objects` / `colors` / `materials` 传给商品搜索。
- 视频摘要进入图片生成 prompt。
- `video_ref` / `image_ref` 传给 render。
- `summary` / `products` / `style_tags` 等进入 memory save content。

输入校验已覆盖：

- 用户要求视频理解但缺视频时进入 `ask_followup`。
- 有视频但文本是普通聊天时保持 `direct_chat`，不会强制理解视频。

## 5. Smoke 能力

已新增：

```text
scripts/smoke_video_understanding.py
```

默认 mock smoke：

```bash
python scripts/smoke_video_understanding.py --video-ref mock://video/product-demo --text "总结这个视频"
```

安全边界：

- import 脚本不触发 Provider。
- 默认 mock 离线运行。
- `http` 缺配置时输出 `provider_unconfigured`。
- 不输出 API Key、Authorization、Bearer token 或完整 base64。
- 不自动下载 URL，不自动上传真实视频。

## 6. Eval / API / WebSocket / Demo 覆盖

Eval 覆盖已扩展：

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

API 覆盖：

```text
tests/test_video_api.py
```

WebSocket 覆盖：

```text
tests/test_websocket_graph_runtime.py
```

Demo runner 覆盖：

```text
demo_data/scenarios/e2e_demo_scenarios.json
tests/test_video_demo_runner.py
```

新增/更新的关键测试：

```text
tests/test_video_understanding_adapter_contract.py
tests/test_video_understanding_contract.py
tests/test_video_understanding_tool.py
tests/test_video_provider_selection.py
tests/test_video_provider_safety.py
tests/test_video_multistep_integration.py
tests/test_video_tool_input_builder.py
tests/test_video_capability_validator.py
tests/test_video_smoke_script.py
tests/test_video_api.py
tests/test_video_evals.py
tests/test_video_demo_runner.py
```

## 7. 安全边界

当前满足：

- 默认 pytest 不调用真实 Video Provider。
- 默认 eval 不调用真实 Video Provider。
- 默认 demo runner 不调用真实 Video Provider。
- 默认 smoke 使用 mock。
- 不提交真实视频。
- 不提交视频帧。
- 不提交真实 Provider raw response。
- 不写入 API Key。
- 不输出 Authorization / Bearer / base64。
- 不自研视频模型。
- 不实现复杂抽帧系统。
- 不实现 WebRTC。
- 不建设视频数据库。

## 8. 已知限制

- `HttpVideoUnderstandingAdapter` 仍是 default-off skeleton，不包含真实 HTTP client。
- Provider retry、fallback、timeout policy、cost guard 和 trace query 仍未统一强化。
- Memory 仍是轻量保存/检索 baseline，未做长期偏好合并或上下文压缩。
- `VisionUnderstandingTool` 保留历史兼容能力，但默认视频能力链路已转向 `VideoUnderstandingTool`。

## 9. Phase 5H 建议

Phase 5H 建议聚焦：

- Provider Safety / Retry / Timeout / Fallback。
- Provider call budget、cost guard 和 latency 观测。
- Trace query 与敏感字段 redaction 强化。
- HTTP skeleton 的真实 Provider 手动 smoke 规范。
- Memory hardening。

Phase 5H 不应默认调用真实外部服务，也不应在没有明确任务时实现 MCP / Skills。

## 验收命令

Task 086 验收命令：

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
git status --short
```

验收结果以任务执行输出为准。
