# Runtime 视觉提醒主动交付实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 视觉条件命中后由 Runtime 立即发布创建时保存的 message，同时补齐提醒生命周期观测，并确定性阻止纯连接级视觉提醒 turn 写入 Mem0。

**Architecture:** Runtime 持有的 `VisualReminderRegistry` 升级为连接级提醒调度边界，负责匹配、调用入口注入的抽象 sender、确认或释放 reminder；Agent-Service 只注册 sender 并把 Runtime 主动消息投影到 WebSocket。Reminder 记录保存创建 turn 的 prompt-safe correlation，生命周期事件回写同一 trace。Memory ingestion 只依据成功 ToolResult 的结构化工具身份跳过纯视觉提醒管理 turn，不解析用户文本。

**Tech Stack:** Python 3.12、asyncio、Pydantic、现有 TraceStore、pytest（mock/offline）

## Global Constraints

- 不在提醒命中后再次调用 LLM；直接发布创建时保存的 `message`。
- Runtime 拥有提醒匹配、状态转换和主动消息发布；Agent-Service 仅拥有 WebSocket 投影。
- 不增加关键词、正则或自然语言意图规则。
- 不调用真实 Provider；测试使用现有 mock embedding provider。
- 保留工作区已有未提交改动，不自动提交。

---

### Task 1: Runtime 主动提醒调度

**Files:**
- Modify: `src/assistant_agent/media/video/visual_reminder.py`
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Test: `tests/tdd/visual-reminder/test_visual_reminder_observer.py`
- Test: `tests/tdd/visual-reminder/test_agent_service_visual_reminder.py`

**Interfaces:**
- Produces: `VisualReminderRegistry.register(manager, *, sender)`；`await VisualReminderRegistry.publish_image_event(user_id, session_id, event)`。
- Consumes: Agent-Service 注入的 `VisualReminderSender`；observer 已生成的 image `EmbeddingEvent`。

- [ ] **Step 1: 写失败测试**

  断言 observer 把已选关键帧 embedding 交给 Runtime registry，sender 在后续 VLM 工作开始前收到预存 message；发送成功后状态为 `triggered`，失败时恢复 `pending`。

- [ ] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-reminder/test_visual_reminder_observer.py tests/tdd/visual-reminder/test_agent_service_visual_reminder.py`

  Expected: FAIL，因为 registry 尚不拥有 sender 和异步 publish 接口。

- [ ] **Step 3: 最小实现**

  将 `reserve_matches -> sender -> confirm/release` 从 observer 移入 Runtime 持有的 registry；observer 在 `_enqueue_semantic_selection` 中先 await 主动消息发布，再排入后续观察队列。Agent-Service 注册连接时仅把 WebSocket sender 注入 registry。

- [ ] **Step 4: 运行 GREEN**

  Run: 与 Step 2 相同。

  Expected: PASS。

### Task 2: 提醒生命周期可观测性

**Files:**
- Create: `src/assistant_agent/media/video/visual_reminder_observability.py`
- Modify: `src/assistant_agent/media/video/visual_reminder.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_reminder_tool.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Test: `tests/tdd/visual-reminder/test_visual_reminder_tool.py`
- Test: `tests/tdd/visual-reminder/test_visual_reminder_observer.py`

**Interfaces:**
- Produces: prompt-safe canonical events `visual_reminder.created`、`visual_reminder.matched`、`visual_reminder.delivery.finished`、`visual_reminder.cleared`。
- Consumes: 创建 turn 的 trace/run/user/session correlation；不记录 target、message、向量或媒体内容。

- [ ] **Step 1: 写失败测试**

  使用 `InMemoryTraceStore` 断言 created → matched → delivery.finished 的顺序、成功/失败状态，以及连接清理产生 cleared；断言 attributes 只有 ID、状态、相似度和计数等 prompt-safe 字段。

- [ ] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-reminder`

  Expected: FAIL，因为当前没有提醒 lifecycle canonical events。

- [ ] **Step 3: 最小实现**

  在 Runtime 初始化后把 TraceStore 绑定到 registry；创建时保存 correlation，匹配、发送终态和清理时追加同 trace 的 late event。观测失败必须 fail-open，不能阻断提醒。

- [ ] **Step 4: 运行 GREEN**

  Run: 与 Step 2 相同。

  Expected: PASS。

### Task 3: 纯视觉提醒 turn 确定性跳过 Mem0

**Files:**
- Modify: `src/assistant_agent/memory/service.py`
- Test: `tests/tdd/visual-reminder/test_visual_reminder_memory_ingestion.py`

**Interfaces:**
- Produces: `memory_ingestion={"status":"skipped","reason":"connection_scoped_visual_reminder"}`。
- Consumes: `AgentState.tool_results` 中成功的 `visual_reminder_manage` 结构化结果，不读取 request/response 文本。

- [ ] **Step 1: 写失败测试**

  构造 completed state：仅成功执行 `visual_reminder_manage` 时不调用 Mem0 client；普通无工具 turn 仍入队；混合其他 ToolResult 的 turn 不被该窄规则整体跳过。

- [ ] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-reminder/test_visual_reminder_memory_ingestion.py`

  Expected: FAIL，因为当前所有 completed text turn 都入队。

- [ ] **Step 3: 最小实现**

  在 `_completed_turn` 之前检查成功 ToolResult 集合；仅当集合非空且全部为 `visual_reminder_manage` 时结构化跳过，并记录稳定 reason。不得匹配用户或助手文本。

- [ ] **Step 4: 运行 GREEN**

  Run: 与 Step 2 相同。

  Expected: PASS。

### Task 4: 权威文档与整体验证

**Files:**
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`

**Interfaces:**
- Produces: Runtime/entry ownership、主动消息时序、观测事件和 Mem0 排除边界的当前权威说明。

- [ ] **Step 1: 同步文档**

  明确 Runtime 直接发布预存 message，Agent-Service 只投影；纯视觉提醒管理 turn 不进入 Mem0；混合 turn 仍交给 Mem0 原生 inference。

- [ ] **Step 2: 运行 feature 测试**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-reminder`

  Expected: PASS。

- [ ] **Step 3: 运行静态检查**

  Run: `git diff --check -- src/assistant_agent/media/video src/assistant_agent/api/agent_service_websocket.py src/assistant_agent/runtime/runtime.py src/assistant_agent/memory/service.py tests/tdd/visual-reminder docs/multimodal-embedding-architecture.md docs/tool-calling-architecture.md docs/memory-service-architecture.md docs/media-agent-service-websocket.md`

  Expected: 无输出，exit 0。

