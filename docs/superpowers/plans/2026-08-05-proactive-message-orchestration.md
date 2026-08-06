# 主动消息编排实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将视觉提醒从 Runtime 内的原始 sender callback 升级为通用、可治理的主动消息契约，并让已发送通知成为短期 session context，而不进入 Mem0 或触发第二次 LLM。

**Architecture:** Runtime 的视觉 matcher 只创建 `ProactiveMessage` 并提交给连接级 orchestrator；orchestrator 异步调用 channel `ProactiveMessageSink`、执行超时和状态确认，避免阻塞视频语义流水线。Agent-Service 提供 WebSocket sink 和普通 chat 的投递仲裁；Runtime 保存有界的已发送 session event，并在下一轮请求中投影为可信 runtime evidence。

**Tech Stack:** Python 3.12、asyncio、Pydantic、现有 Runtime/Context/TraceStore、pytest mock/offline。

## Global Constraints

- 不在提醒命中后再次调用 LLM；正文只能来自创建提醒时已保存的 message。
- Runtime 拥有匹配、主动消息状态和 session event；Agent-Service 只实现 channel sink 与连接内投递仲裁。
- 视觉提醒保持 `connection_ephemeral`，断线清除，不写 durable outbox 或 Mem0。
- channel 成功只表达 `server_transport` sent，不伪装成客户端 ACK。
- 所有新增测试位于现有 `tests/tdd/visual-reminder`，真实 Provider 不进入 pytest。
- 保留当前脏工作区，不自动提交。

---

### Task 1: 主动消息公共契约

**Files:**
- Create: `src/assistant_agent/runtime/proactive_messages.py`
- Modify: `src/assistant_agent/media/video/visual_reminder.py`
- Test: `tests/tdd/visual-reminder/test_proactive_messages.py`

**Interfaces:**
- Produces: `ProactiveMessage`、`ProactiveDeliveryAttempt`、`ProactiveMessageSink`、`ProactiveSessionEventStore`。
- Consumes: reminder ID、user/session identity、预存 message。

- [x] 写契约和有界 session event store 的失败测试。
- [x] 运行定向测试，确认因接口不存在而 RED。
- [x] 实现不可变模型、sink protocol 与 identity-scoped bounded store。
- [x] 运行定向测试，确认 GREEN。

### Task 2: Runtime 异步主动消息编排

**Files:**
- Modify: `src/assistant_agent/media/video/visual_reminder.py`
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Test: `tests/tdd/visual-reminder/test_visual_reminder_manager.py`
- Test: `tests/tdd/visual-reminder/test_visual_reminder_observer.py`

**Interfaces:**
- Consumes: `VisualReminderRegistry.register(manager, sink=...)`。
- Produces: `publish_image_event()` 只完成 reserve/dispatch；后台完成 sent/failed/cancelled、confirm/release 和 session event。

- [x] 写不阻塞视觉队列、成功回写 session event、失败/超时恢复 pending 的失败测试。
- [x] 运行测试并确认 RED。
- [x] 将直接 await sender 改为 Runtime-owned delivery task，并实现 bounded timeout/close cleanup。
- [x] 运行测试并确认 GREEN。

### Task 3: Agent-Service sink 与 session context

**Files:**
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/context/renderer.py`
- Test: `tests/tdd/visual-reminder/test_agent_service_visual_reminder.py`
- Test: `tests/tdd/visual-reminder/test_proactive_messages.py`

**Interfaces:**
- Produces: WebSocket `ProactiveMessageSink`；`proactive_session_context` runtime metadata。
- Consumes: 活动 chat task 集合、Runtime session event store。

- [x] 写独立 chatIndex、等待活动 chat 完成、下一轮 Provider context 可见的失败测试。
- [x] 运行测试并确认 RED。
- [x] 实现 sink 投影/仲裁，并在 Runtime run 起点附加有界可信 session context。
- [x] 运行测试并确认 GREEN。

### Task 4: 文档与验证

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/context_engineering_status.md`

- [x] 同步 proactive message、ephemeral/durable 边界、server-sent 语义与 session context。
- [x] 运行 `tests/tdd/visual-reminder`。
- [x] 运行受影响的 `OBS-001` core tests 和默认 core suite。
- [x] 运行 `git diff --check`、Ruff 与相关模块编译检查。
