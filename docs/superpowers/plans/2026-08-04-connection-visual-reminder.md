# 连接级视觉提醒实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在可信 Agent-Service VIDEO WebSocket 连接内提供可创建、查看、取消的多条一次性视觉提醒，复用已选关键帧的 SigLIP2 image embedding，并通过当前连接即时发送 `chatResponse`。

**Architecture:** 新增纯状态 `VisualReminderManager` 和 owner/session registry；Tool 使用 runtime identity 找到 manager，并通过现有 `SessionEmbeddingCoordinator` 只计算一次 target text embedding。`RealtimeVideoObserver` 在关键帧选中回调中复用现有 `EmbeddingEvent` 预留命中项，把投递异步交给 Agent-Service 的串行 `_send_response`，成功后确认一次性终态，失败时按连接状态恢复或清理。

**Tech Stack:** Python 3.11、Pydantic v2、asyncio、FastAPI WebSocket、现有 SigLIP2 embedding coordinator/comparator、pytest。

## Global Constraints

- 默认 Python：`/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。
- 所有 pytest 显式设置 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 SigLIP2、VLM、联网 Provider 或外部服务。
- 不新增依赖，不读取或写入真实凭据。
- 提醒仅属于当前可信 Agent-Service `callType=VIDEO` 连接；断线清空，不进入 Memory、durable task 或 proactive wake。
- 每连接最多 16 条 `pending/reserved`，最多保留最近 64 条终态记录；默认 cosine threshold 为 `0.82`。
- 只匹配最终已选关键帧；复用现有 image `EmbeddingEvent`，不新增 image embedding，不调用 VLM 复核。
- Tool exposure 只依据可信 entry profile、VIDEO call type 和活动 manager 等结构化事实，不使用关键词或正则。
- 不修改 `tests/core`：`Core invariant: unchanged.`；临时 RED/GREEN 测试放入可手动整目录删除的 `tests/tdd/visual-reminder/`。
- 工作区已有 3D 投递等用户改动；实施提交只能包含本功能文件，不能回滚或夹带这些改动。

---

## 文件结构

- Create `src/assistant_agent/media/video/visual_reminder.py`：提醒 model、线程安全 manager、owner/session registry 和 reservation 状态机。
- Create `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_reminder_tool.py`：`visual_reminder_manage` 输入输出模型和受治理 Tool。
- Modify `src/assistant_agent/tools/ids.py`：新增稳定 Tool name。
- Modify `src/assistant_agent/tools/plugins/contracts.py`、`registry_factory.py`、`media_inspection/plugin.py`：把 reminder registry 注入 Tool composition。
- Modify `src/assistant_agent/config/__init__.py`：增加 threshold、active limit、terminal history limit 配置和环境变量校验。
- Modify `src/assistant_agent/context/tool_exposure.py`：加入可信活动 reminder manager exposure fact。
- Modify `src/assistant_agent/runtime/runtime.py`：拥有 registry、覆盖调用方伪造 fact、向默认 registry 注入依赖。
- Modify `src/assistant_agent/runtime/system_prompt_policy.py`：仅在 Tool 已暴露时说明 create/list/cancel 使用方式。
- Modify `src/assistant_agent/media/video/realtime_video_observer.py`：接收 manager 和 async delivery callback，复用选中事件并拥有投递任务生命周期。
- Modify `src/assistant_agent/api/agent_service_websocket.py`：VIDEO handshake 注册 manager、metadata 传播 call type、构造 reminder `chatResponse`、observer 注入和断线注销。
- Create `tests/tdd/visual-reminder/test_visual_reminder_manager.py`：状态、容量、匹配、并发和失败恢复 RED/GREEN。
- Create `tests/tdd/visual-reminder/test_visual_reminder_tool.py`：Tool schema、身份隔离、text embedding、结构化 exposure RED/GREEN。
- Create `tests/tdd/visual-reminder/test_visual_reminder_observer.py`：只匹配已选帧、复用向量、VLM 隔离和投递任务 RED/GREEN。
- Create `tests/tdd/visual-reminder/test_agent_service_visual_reminder.py`：握手、即时 `chatResponse`、video 切换和断线清理 RED/GREEN。
- Modify `docs/multimodal-embedding-architecture.md`、`docs/media-agent-service-websocket.md`、`docs/tool-calling-architecture.md`：同步当前事实。

---

### Task 1: 连接级提醒状态机与配置

**Files:**
- Create: `src/assistant_agent/media/video/visual_reminder.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Test: `tests/tdd/visual-reminder/test_visual_reminder_manager.py`

**Interfaces:**
- Consumes: `EmbeddingEvent`、`EmbeddingComparator.similarity(left, right)`。
- Produces: `VisualReminderManager.create(target, message, target_embedding)`、`list_records()`、`cancel(reminder_id)`、`reserve_matches(image_event)`、`confirm(reminder_id, reservation_id)`、`release(reminder_id, reservation_id)`、`close()`；`VisualReminderRegistry.register/peek/unregister`。

- [ ] **Step 1: 写 manager 基础行为的失败测试**

```python
def test_manager_supports_multiple_deduplicated_one_shot_reminders():
    manager = VisualReminderManager(user_id="u1", session_id="s1", similarity_threshold=0.82)
    first = manager.create(target="水已经烧开", message="水烧开了", target_embedding=_text("t1", [1.0, 0.0]))
    duplicate = manager.create(target=" 水已经烧开 ", message="水烧开了", target_embedding=_text("t2", [1.0, 0.0]))
    second = manager.create(target="有人进门", message="有人进门了", target_embedding=_text("t3", [0.0, 1.0]))

    assert duplicate.reminder_id == first.reminder_id
    assert len(manager.list_records()) == 2
    reserved = manager.reserve_matches(_image("frame-1", [0.9, 0.1]))
    assert [item.reminder_id for item in reserved] == [first.reminder_id]
    assert manager.confirm(first.reminder_id, reservation_id=reserved[0].reservation_id).status == "triggered"
    assert manager.reserve_matches(_image("frame-2", [1.0, 0.0])) == []
    assert second.status == "pending"
```

- [ ] **Step 2: 运行基础测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-reminder/test_visual_reminder_manager.py`

Expected: collection fails because `assistant_agent.media.video.visual_reminder` does not exist.

- [ ] **Step 3: 实现最小 model、manager 和 registry**

```python
class VisualReminderRecord(BaseModel):
    reminder_id: str
    target: str
    message: str
    target_embedding: EmbeddingEvent = Field(exclude=True)
    created_at_ms: int
    status: Literal["pending", "reserved", "triggered", "cancelled"] = "pending"
    reservation_id: str | None = None

class VisualReminderReservation(BaseModel):
    reminder_id: str
    reservation_id: str
    target: str
    message: str
    similarity: float
```

在同一文件中实现以下确切签名：

- `create(*, target: str, message: str, target_embedding: EmbeddingEvent) -> VisualReminderPublicRecord`
- `reserve_matches(image_event: EmbeddingEvent) -> list[VisualReminderReservation]`
- `confirm(reminder_id: str, *, reservation_id: str) -> VisualReminderOperation`
- `release(reminder_id: str, *, reservation_id: str) -> VisualReminderOperation`
- `cancel(reminder_id: str) -> VisualReminderOperation`
- `list_records() -> list[VisualReminderPublicRecord]`
- `close() -> None`
- registry 的 `register(manager) -> None`、`peek(user_id, session_id) -> VisualReminderManager | None`、`unregister(user_id, session_id, *, manager) -> bool`。

锁内执行状态检查和 `pending -> reserved`；只把 compatibility error 限制在单条提醒；terminal 记录超过 64 条时按终态时间淘汰最旧项。

- [ ] **Step 4: 增加容量、取消竞争、release、close 和 registry identity 测试并逐个 GREEN**

```python
def test_cancel_cannot_win_after_reservation():
    reservation = manager.reserve_matches(_image("frame", [1.0, 0.0]))[0]
    result = manager.cancel(reservation.reminder_id)
    assert result.status == "reserved"
    assert result.changed is False

def test_release_requires_matching_reservation_identity():
    assert manager.release(reminder_id, reservation_id="wrong").status == "reserved"
    assert manager.release(reminder_id, reservation_id=reservation.reservation_id).status == "pending"

def test_close_rejects_create_and_clears_records():
    manager.close()
    with pytest.raises(VisualReminderClosedError):
        manager.create(target="x", message="y", target_embedding=_text("t", [1.0, 0.0]))
    assert manager.list_records() == []
```

- [ ] **Step 5: 加入并校验配置**

在 `ProviderConfig` 和 `from_env()` 增加：

```python
visual_reminder_similarity_threshold: float = 0.82
visual_reminder_max_active: int = 16
visual_reminder_terminal_history_limit: int = 64
```

环境变量分别为 `REALTIME_VISUAL_REMINDER_SIMILARITY_THRESHOLD`、`REALTIME_VISUAL_REMINDER_MAX_ACTIVE`、`REALTIME_VISUAL_REMINDER_TERMINAL_HISTORY_LIMIT`；threshold 必须在 `[0, 1]`，两个 limit 必须为正整数。

- [ ] **Step 6: 运行 Task 1 测试并提交**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-reminder/test_visual_reminder_manager.py`

Expected: PASS.

Commit: `feat: add connection visual reminder state`

---

### Task 2: 受治理 Tool、runtime ownership 与结构化 exposure

**Files:**
- Create: `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_reminder_tool.py`
- Modify: `src/assistant_agent/tools/ids.py`
- Modify: `src/assistant_agent/tools/plugins/contracts.py`
- Modify: `src/assistant_agent/tools/plugins/registry_factory.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py`
- Modify: `src/assistant_agent/context/tool_exposure.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/system_prompt_policy.py`
- Test: `tests/tdd/visual-reminder/test_visual_reminder_tool.py`

**Interfaces:**
- Consumes: Task 1 `VisualReminderRegistry`、`SessionEmbeddingCoordinatorStore`、runtime identity binding。
- Produces: registered `visual_reminder_manage` Tool and `_trusted_visual_reminder_available` run fact。

- [ ] **Step 1: 写 Tool create/list/cancel 与 identity 隔离的失败测试**

```python
def test_tool_creates_target_embedding_in_current_runtime_session():
    result = tool.run(
        {"action": "create", "target": "水已经烧开", "message": "水烧开了", "session_id": "s1"},
        ToolContext(user_id="u1", session_id="s1", run_id="r1"),
    )
    assert result.success is True
    assert result.data["status"] == "pending"
    assert "target_embedding" not in result.data
    assert registry.peek("u2", "s1") is None
```

另写 `list` 只返回公共字段、`cancel` 稳定返回 `not_found/reserved/triggered/cancelled`、manager 或 text readiness 不可用时结构化失败的测试。

- [ ] **Step 2: 运行 Tool 测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-reminder/test_visual_reminder_tool.py`

Expected: collection fails because `visual_reminder_tool` and Tool ID do not exist.

- [ ] **Step 3: 实现 Tool schema 和执行**

```python
class VisualReminderManageInput(BaseModel):
    action: Literal["create", "list", "cancel"]
    target: str | None = Field(default=None, min_length=1, max_length=500)
    message: str | None = Field(default=None, min_length=1, max_length=500)
    reminder_id: str | None = Field(default=None, min_length=1)
    session_id: str = ""

class VisualReminderManageTool(ToolBase):
    name = VISUAL_REMINDER_MANAGE_TOOL_NAME
    category = "write"
    requires_media = []
    runtime_input_bindings = (
        RuntimeInputBinding(field="session_id", source="runtime_identity", key="session_id"),
    )
```

`model_validator` 对 action 做互斥字段校验。`create` acquire coordinator lease、调用一次 `embed_text(TextObservation(session_id=input.session_id, observation_id=f"visual-reminder:{context.run_id}:{uuid4().hex}", text=input.target, source="user_text"))`，仅在成功 `EmbeddingEvent` 时创建记录；`list/cancel` 不计算 embedding。

- [ ] **Step 4: 接入 plugin composition 和 runtime ownership**

给 `ToolPluginContext`、`create_default_registry()` 增加 `visual_reminder_registry`；`AgentGraphRuntime` 构造并拥有 registry，默认 Tool registry 组装时注入。`MediaInspectionPlugin` 在 coordinator store 和 reminder registry 都存在时注册 Tool，不依赖 VLM readiness。

- [ ] **Step 5: 写结构化 exposure 的失败测试**

```python
def test_visual_reminder_tool_requires_trusted_video_connection_manager():
    request = UserRequest(user_id="u1", session_id="s1", text="提醒", metadata=_trusted_agent_service_metadata())
    assert evaluate_tool_exposure(request, spec).excluded_reasons == ("visual_reminder_connection_not_available",)
    request.metadata["_trusted_visual_reminder_available"] = True
    assert evaluate_tool_exposure(request, spec).exposed is True
```

同时验证调用方传入 `_trusted_visual_reminder_available=True` 会被 runtime 覆盖为 false，以及非 Agent-Service / AUDIO call type 不暴露。

- [ ] **Step 6: 实现可信 fact 刷新和 prompt policy**

`AgentGraphRuntime` 在 catalog 构建前删除调用方同名字段，再要求：可信 Agent-Service、metadata `agent_service.call_type == "VIDEO"`、registry 中同 owner/session manager 存在且活动，才写入 true。`tool_exposure_facts()` 读取该布尔值，并对 Tool ID应用专用 exclusion reason。

系统提示只描述：当本轮已提供 `visual_reminder_manage` 时，用户要创建、查看或取消当前视频连接的视觉提醒应调用它；不得猜测创建成功。

- [ ] **Step 7: 运行 Task 2 测试并提交**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-reminder/test_visual_reminder_tool.py`

Expected: PASS.

Commit: `feat: add governed visual reminder tool`

---

### Task 3: 已选关键帧向量复用与异步投递状态

**Files:**
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Test: `tests/tdd/visual-reminder/test_visual_reminder_observer.py`

**Interfaces:**
- Consumes: Task 1 `VisualReminderManager.reserve_matches/confirm/release`。
- Produces: observer constructor args `visual_reminder_manager` and `visual_reminder_sender`；sender signature `async (VisualReminderPublicRecord) -> None`。

- [ ] **Step 1: 写关键帧复用和一次性投递的失败测试**

```python
async def test_selected_keyframe_reuses_embedding_and_sends_once(tmp_path):
    coordinator = CountingCoordinator()
    manager = _manager_with_matching_reminder()
    sent = []
    observer = _observer(coordinator=coordinator, manager=manager, sender=_append_async(sent))

    await observer.submit(_frame(tmp_path, sequence=1))
    await observer.wait_idle()

    assert coordinator.image_calls == 1
    assert [item.message for item in sent] == ["水烧开了"]
    assert manager.list_records()[0].status == "triggered"
```

另写语义未选帧不发送、embedding failure 降级帧不发送、同一帧多命中、sender 失败后恢复 pending、sender 异常不阻断 VLM snapshot 的测试。

- [ ] **Step 2: 运行 observer 测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-reminder/test_visual_reminder_observer.py`

Expected: constructor rejects reminder args or no reminder is sent.

- [ ] **Step 3: 实现选中回调匹配与 observer-owned delivery task**

在 `_enqueue_semantic_selection(frame, event, reason)` 中：

```python
if event is not None and self.visual_reminder_manager is not None:
    for reservation in self.visual_reminder_manager.reserve_matches(event):
        self._start_visual_reminder_delivery(reservation)
await self._enqueue(
    frame,
    enqueued_ns=enqueued_ns,
    keyframe_selection_latency_ms=0,
    already_retained=True,
)
```

每个 task 调用 async sender；成功后以 `reservation_id` confirm，异常且 observer/manager 仍活动时 release。task 集合由 observer 拥有，done callback 消费异常；`wait_idle()` 等待当前 delivery tasks，`close()` 阻止新 task、取消/等待现有 task，并对未确认 reservation 做 release。提醒匹配和 task 创建异常只记录清理后的观测，不能阻断 `_enqueue()`。

- [ ] **Step 4: 运行 observer 和现有统一 SigLIP2 相关定向测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/visual-reminder/test_visual_reminder_observer.py \
  tests/tdd/unified-siglip2/test_realtime_visual_semantic_publication.py
```

Expected: PASS；计数断言证明没有新增 image embedding。

- [ ] **Step 5: 提交**

Commit: `feat: match visual reminders on selected keyframes`

---

### Task 4: Agent-Service VIDEO 生命周期与即时 chatResponse

**Files:**
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Test: `tests/tdd/visual-reminder/test_agent_service_visual_reminder.py`

**Interfaces:**
- Consumes: runtime-owned registry、Task 3 observer sender callback、现有 `_send_response()` serialization。
- Produces: successful VIDEO handshake registration, reminder response envelope, disconnect unregister。

- [ ] **Step 1: 写 handshake、metadata 和 response payload 的失败测试**

```python
async def test_video_control_registers_connection_manager_and_disconnect_clears_it():
    response = await AssistantControlHandler().handle(
        session_id="vendor",
        body={"number": "u1", "callType": "VIDEO"},
        state=state,
    )
    manager = runtime.visual_reminder_registry.peek("u1", state.runtime_session_id)
    assert json.loads(response["body"])["code"] == 0
    assert manager is state.visual_reminder_manager

    await _cleanup_agent_service_connection(state, gateway_manager=gateway, close_code=1000, close_reason=None)
    assert runtime.visual_reminder_registry.peek("u1", state.runtime_session_id) is None
    assert manager.list_records() == []
```

另写 AUDIO 不注册、重复 VIDEO control 幂等替换前清理、`_agent_service_gateway_metadata()` 包含 `call_type=VIDEO`、reminder response 使用 `chatIndex=visual-reminder:<id>` 和媒体 `intentResult.status=SUCCESS` 的测试。

- [ ] **Step 2: 运行 Agent-Service 测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-reminder/test_agent_service_visual_reminder.py`

Expected: state lacks manager and reminder response builder.

- [ ] **Step 3: 实现 handshake 注册和 metadata**

`AgentServiceConnectionState` 新增 `assistant_control_call_type`、`visual_reminder_manager`。VIDEO handshake 成功初始化 Gateway session 后，从 shared runtime 获取 registry，根据 config 构造 manager 并注册；AUDIO 不注册。后续 chat metadata 显式写入 `agent_service.call_type`，不从自然语言或是否已有 video frame 推断。

- [ ] **Step 4: 实现主动 chatResponse sender 并注入 observer**

```python
def _visual_reminder_chat_response(state, reminder):
    return _response_envelope(
        message="chatResponse",
        session_id=state.response_session_id,
        body={"message": {"chatIndex": f"visual-reminder:{reminder.reminder_id}",
              "content": {"intentResult": {"description": reminder.message, "status": "SUCCESS"}}},
              **_display_flags(False)},
    )
```

observer factory 接收 state/manager/sender；sender 调用现有 `_send_response()`，因此与 chat、ACK 共用 `state.send_lock`。若 `state.closed` 或发送失败则抛出，让 observer release reservation。

- [ ] **Step 5: 实现 video 切换保留与 disconnect 清理**

切换 `video_id` 时新 observer 继续注入同一个 connection manager。连接 cleanup 首先设置 `state.closed=True` 并关闭 manager，随后关闭 observer/发送任务，最后用精确 manager identity 从 registry 注销；任何清理步骤异常都不能遗留活动 manager。

- [ ] **Step 6: 运行 Agent-Service 与 observer 测试并提交**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/visual-reminder/test_agent_service_visual_reminder.py \
  tests/tdd/visual-reminder/test_visual_reminder_observer.py
```

Expected: PASS.

Commit: `feat: deliver visual reminders over agent service`

---

### Task 5: 权威文档同步与完成验证

**Files:**
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/tool-calling-architecture.md`
- Verify: all files changed by Tasks 1–4

**Interfaces:**
- Consumes: 已通过测试证明的最终实现。
- Produces: 当前架构、协议和 Tool 治理事实权威；完整验证证据。

- [ ] **Step 1: 更新多模态 embedding 权威文档**

把主动提醒从非目标移除，并记录：只匹配最终已选关键帧、复用 image event、target 只计算一次 text embedding、阈值 `0.82`、多条一次性、无 VLM 和连接清理。

- [ ] **Step 2: 更新 Agent-Service 协议权威文档**

记录 VIDEO handshake 创建连接级 manager；主动 `chatResponse` 的 `chatIndex=visual-reminder:<id>`、`intentResult` payload、与普通响应共享串行发送、video 切换保留和断线清理。明确这不是跨连接 delivery/ACK 契约。

- [ ] **Step 3: 更新 Tool 权威文档**

记录 `visual_reminder_manage` 的 `write` category、create/list/cancel schema ownership、runtime identity 注入、结构化 exposure 和完整治理执行链。

- [ ] **Step 4: 运行完整 feature TDD 集合**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/visual-reminder
```

Expected: PASS.

- [ ] **Step 5: 运行受影响的现有统一 embedding 定向集合**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2/test_embedding_config.py \
  tests/tdd/unified-siglip2/test_embedding_runtime_lifecycle.py \
  tests/tdd/unified-siglip2/test_realtime_visual_semantic_publication.py \
  tests/tdd/unified-siglip2/test_visual_attention_consumer.py
```

Expected: PASS.

- [ ] **Step 6: 静态检查和 scope 审计**

Run:

```bash
git diff --check
git status --short
git diff --name-only HEAD~4..HEAD
```

确认没有真实 Provider 调用，没有向量出现在 response/log schema，没有修改 `tests/core`，且提交不包含原工作区 3D 投递改动。

- [ ] **Step 7: 最终提交文档**

Commit: `docs: document connection visual reminders`

- [ ] **Step 8: 完成逐项审计**

逐条把设计规格的 6 个验收不变量映射到源码与测试证据：单 image embedding、仅关键帧、无 VLM、多条一次性、串行 `chatResponse`、断线不可恢复。只有全部证据充分时才报告完成。
