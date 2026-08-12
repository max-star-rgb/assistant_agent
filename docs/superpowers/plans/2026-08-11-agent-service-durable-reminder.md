# Agent-Service 持久任务主动提醒实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户通过 `/agent-service/v1` 自然语言创建酒店价格监控，任务命中或截止后通过当前或重连后的 Media-Agent WebSocket 主动收到提醒。

**Architecture:** Durable task 与 notification outbox 继续拥有持久状态；新增 Agent-Service connection hub 只保存当前进程内的在线投递租约。Notification worker 在收件人离线时无损延期，在线时通过 Agent-Service transport 发送独立 `chatResponse`。Simulator 使用显式监听模式等待目标 `task://` 的主动消息并在服务重启后自动重连。

**Tech Stack:** Python 3.12、FastAPI WebSocket、Pydantic、SQLite、asyncio、pytest。

## Global Constraints

- 自动测试固定使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得访问真实百炼或 FlyAI。
- 所有本地 Tool Call 继续经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- Agent-Service 只拥有连接投递，不拥有 durable task 状态或新的 Agent loop。
- 不通过关键词、正则或入口解析推断用户意图；由真实 LLM 选择 `hotel_price_watch_create`。
- 第一版送达保证止于 `server_transport`，不新增 proactive `chatResponseAck` 重放协议。
- 保留工作区既有未提交改动，不回滚、不提交本计划或实现。

---

### Task 1: Notification worker 离线无损延期

**Files:**
- Modify: `src/assistant_agent/automation/proactive_wake/delivery.py`
- Test: `tests/tdd/agent-service-durable-reminder/test_notification_delivery.py`

**Interfaces:**
- Consumes: `NotificationOwner` 与 `SQLiteProactiveWakeStore.defer_notification(...)`。
- Produces: `NotificationRecipientAvailability.is_available(owner) -> Awaitable[bool]`；`NotificationDeliveryWorker(..., recipient_availability=..., unavailable_retry_seconds=...)`。

- [ ] 写入失败测试：收件人离线时通知转为 `retry_wait`、`attempt_count == 0`、`last_reason_code == "recipient_offline"`。
- [ ] 显式运行该测试并确认因构造参数不存在而 RED。
- [ ] 在 worker 开始 delivery attempt 前查询 availability；离线时调用 `defer_notification`，不创建 attempt。
- [ ] 重跑测试并确认 GREEN。

### Task 2: Agent-Service 在线连接 hub 与 transport

**Files:**
- Create: `src/assistant_agent/api/agent_service_notifications.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `src/assistant_agent/api/app.py`
- Test: `tests/tdd/agent-service-durable-reminder/test_agent_service_notification.py`

**Interfaces:**
- Produces: `AgentServiceNotificationHub.register/unregister/is_available/publish`、`AgentServiceNotificationTransport.send/is_available`、`get_agent_service_notification_hub()`。
- Agent-Service sender consumes one `NotificationEnvelope` and returns `DeliveryResult`。

- [ ] 写入失败测试：最新 connection lease 获得通知，旧连接 unregister 不移除新 lease；无连接时不可达。
- [ ] 写入失败测试：`assistantControl` 注册 owner，cleanup 注销 owner；主动包使用 `durable-task:<task_id>` 且等待普通 chat task。
- [ ] 显式运行测试并确认缺少模块/行为而 RED。
- [ ] 实现进程级 hub、latest-wins lease 与 transport。
- [ ] 在控制握手后注册 sender，在连接 cleanup 时按 connection id 注销。
- [ ] 真实/Mock Provider 均在 durable notification worker 启用时装配 Agent-Service transport，并注入 availability。
- [ ] 重跑测试并确认 GREEN。

### Task 3: 酒店监控延迟首查与终态提醒

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/models.py`
- Modify: `src/assistant_agent/automation/durable_tasks/hotel_price_watch.py`
- Test: `tests/tdd/agent-service-durable-reminder/test_hotel_price_watch.py`

**Interfaces:**
- Produces: `HotelPriceWatchGoal.starts_at: datetime | None`，要求带时区且早于 `ends_at`。
- Runtime 在 `now < starts_at` 时返回 `TaskCheckpoint(kind="waiting_schedule")`，不调用酒店 Tool。
- 截止未命中时返回带 `TaskNotificationRequest` 的 completed checkpoint。

- [ ] 写入失败测试：非法 `starts_at` 被拒绝；未来 `starts_at` 产生持久 wait；截止产生通知。
- [ ] 显式运行并确认 RED。
- [ ] 添加 schema 与最小 runtime 分支，未提供 `starts_at` 时保持立即检查。
- [ ] 将默认 `notification_channel` 调整为 `agent_service`，命中与截止通知均保留安全摘要。
- [ ] 重跑测试并确认 GREEN。

### Task 4: Agent-Service 投影 task handle

**Files:**
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Test: `tests/tdd/agent-service-durable-reminder/test_agent_service_notification.py`

**Interfaces:**
- Produces: 成功终包 `outputRefs` 可包含最多 4 个去重的 `workflow://` 与 `task://` 引用。

- [ ] 写入失败测试：混合引用只保留合法 workflow/task refs，不泄漏其他 scheme。
- [ ] 显式运行并确认 RED。
- [ ] 用通用 durable ref 投影替换 workflow-only filter，同时保持图片投影不变。
- [ ] 重跑测试并确认 GREEN。

### Task 5: Media Simulator 主动监听与重连

**Files:**
- Modify: `scripts/media_simulator.py`
- Test: `tests/tdd/agent-service-durable-reminder/test_media_simulator_proactive.py`

**Interfaces:**
- `MediaChatOutcome` 新增 `task_ids`。
- `run_media_console(..., wait_proactive: bool = False)` 在成功创建 task 后监听目标 `durable-task:*`。
- CLI 新增 `--wait-proactive`。

- [ ] 写入失败测试：从 `outputRefs` 解析 task id；忽略非目标提醒；目标提醒打印后返回。
- [ ] 写入失败测试：监听时连接关闭，使用相同 `sessionId + userNumber` 重连后继续接收。
- [ ] 显式运行并确认 RED。
- [ ] 实现 task ref 解析、监听循环、有界退避重连和 CLI 参数。
- [ ] 重跑测试并确认 GREEN。

### Task 6: 权威文档与最小验证

**Files:**
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `scripts/README.md`
- Modify: `.env.example`

**Interfaces:**
- 文档明确 durable notification 属于持久 outbox，Agent-Service hub 只拥有在线投递；Simulator 监听模式与 `server_transport` 保证边界清晰。

- [ ] 更新 Agent-Service 主动 `chatResponse`、`task://` outputRefs、重连与 ACK 限制。
- [ ] 更新 Tool/durable workflow 权威中的酒店监控与通知投递边界。
- [ ] 更新 Simulator 使用说明和三个 durable 开关示例。
- [ ] 运行 `tests/tdd/agent-service-durable-reminder`。
- [ ] 运行受影响的既有 visual-reminder、deep-research Agent-Service 子集。
- [ ] 运行文档 authority validator 与 `git diff --check`。
- [ ] 不在自动测试中调用真实 Provider；将真实百炼 + FlyAI smoke 留给用户最终验收。
