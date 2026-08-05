# 图片转 3D 多入口投递解耦实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `image_generation` 与 `image_to_3d` 可由 Assistant client、HTTP、规范化 Gateway WebSocket 和 MCP 独立调用，只有可信 Agent-Service 入口才把生成结果投影到媒体 WebSocket。

**Architecture:** Runtime 和 Tool 只产生中立 artifact/job；Gateway 入口 capability 决定 job 是否具备媒体投递目标。3D callback 先写入按 `job_id` 关联的 completion registry，再由 Agent-Service adapter 对明确的媒体目标做 vendor `chatResponse` 投影。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、pytest、现有 Gateway/Tool governance。

## Global Constraints

- 默认使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Provider 或 3D 服务。
- 不绕过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- Gateway 不构造 Agent-Service vendor payload；Agent-Service adapter 不承担 Tool 调用和结果事实存储。
- 当前相关文件有用户未提交改动，必须增量保留。
- Core invariant 不变；RED/GREEN 仅进入 `tests/tdd/image-3d-entry-delivery/`。

---

### Task 1: 可信入口能力

**Files:**
- Modify: `src/assistant_agent/gateway/capabilities.py`
- Modify: `src/assistant_agent/api/routes_agent.py`
- Test: `tests/tdd/image-3d-entry-delivery/test_entry_capabilities.py`

**Interfaces:**
- Produces: `EntryAdapterCapabilities.supports_generated_media_delivery: bool`
- Produces: HTTP/Gateway WebSocket 默认 `False`，Agent-Service 固定 `True`

- [ ] 写失败测试，证明只有 Agent-Service capability 开启生成媒体投递。
- [ ] 显式运行测试并确认因字段缺失而失败。
- [ ] 增加 capability，并由各可信 entry adapter 覆盖调用方 metadata。
- [ ] 运行测试确认通过。

### Task 2: 中立 3D job/completion registry

**Files:**
- Create: `src/assistant_agent/media/image_to_3d_completion.py`
- Modify: `src/assistant_agent/media/image_to_3d.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_to_3d/models.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_to_3d/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_to_3d/plugin.py`
- Test: `tests/tdd/image-3d-entry-delivery/test_image_to_3d_completion.py`

**Interfaces:**
- Produces: `ImageTo3DJobRegistry.register(...) -> ImageTo3DJob`
- Produces: `ImageTo3DJobRegistry.complete(job_id, artifact) -> ImageTo3DJob`
- Produces: `ImageTo3DSubmission.job_id`
- Consumes: 可信 `supports_generated_media_delivery` capability，并记录为中立 delivery target

- [ ] 写失败测试，证明提交生成稳定 job、callback correlation 不再使用 runtime session 充当 job ID。
- [ ] 运行测试并确认期望失败。
- [ ] 实现线程安全的进程内 job/completion registry 和中立 artifact model。
- [ ] 让 ToolResult 返回 `job_id/status/source_image_id`，并只透传可信 delivery target。
- [ ] 运行测试确认通过。

### Task 3: callback 接收与媒体投影拆分

**Files:**
- Modify: `src/assistant_agent/api/rendering_3d_callback.py`
- Modify: `src/assistant_agent/media/rendering_3d_relay.py`
- Test: `tests/tdd/image-3d-entry-delivery/test_rendering_3d_callback_routing.py`

**Interfaces:**
- Consumes: `ImageTo3DJobRegistry.complete(...)`
- Produces: callback 成功写入中立 artifact；`delivery_target=none` 不调用 relay；`agent_service` 才投影并发送

- [ ] 写失败测试：非媒体 job 的 callback 返回成功、结果可查询且 sender 未调用。
- [ ] 写失败测试：媒体 job callback 保存相同结果并发送一次 vendor frame。
- [ ] 运行测试，确认当前 callback 的无媒体 500 和无结果存储行为导致失败。
- [ ] 重构 callback 为“接收/保存”优先，再做可选媒体投影。
- [ ] 保留旧 callback URL 的兼容处理，但不得让新 job 回退为隐式媒体投递。
- [ ] 运行测试确认通过。

### Task 4: 非媒体结果读取与文档同步

**Files:**
- Modify: `src/assistant_agent/api/rendering_3d_callback.py` 或新增窄 job query route
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/tool-calling-architecture.md`
- Test: `tests/tdd/image-3d-entry-delivery/test_image_to_3d_completion.py`

**Interfaces:**
- Produces: 受 owner/session 约束的 job 状态与最终 artifact 读取边界

- [ ] 写失败测试，证明非媒体调用方可用 `job_id` 读取完成结果，其他 owner 不可读取。
- [ ] 实现最小 owner-bound 查询边界。
- [ ] 更新三份权威文档，明确 Runtime、Gateway、completion service 和 entry adapter 职责。
- [ ] 运行 feature TDD 全集与 `git diff --check`。
- [ ] 审查相关 diff，确保没有真实 Provider 调用、媒体默认投递或用户改动回滚。
