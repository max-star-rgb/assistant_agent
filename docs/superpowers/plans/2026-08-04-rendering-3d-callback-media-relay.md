# 3D 服务回调媒体转发实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 3D 服务回调通过原媒体 WebSocket 投递 `TD_MODEL` 或 `VIDEO` 类型的完整 `chatResponse`。

**Architecture:** 新增一个进程内 `Rendering3DRelayRegistry`，以 runtime session ID 关联活动媒体连接的异步发送函数、连接身份和手机号。`image_to_3d` 将当前可信 `chatIndex` 写入回调 URL；HTTP 回调完成 schema 校验和媒体类型映射后，经 registry 复用 WebSocket 的既有发送锁投递，发送成功后才 ACK。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、asyncio、pytest/TestClient。

## Global Constraints

- 默认使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，测试不得访问真实 Provider、3D 服务或网络。
- 现有未提交改动属于用户，只做相关增量修改，不回滚或覆盖。
- `ply/glb` 映射为 `TD_MODEL/modelUrl`；`mp4` 映射为 `VIDEO/videoUrl`。
- 回调不创建 Agent turn、不调用 LLM、不下载或保存 `mediaUrl` 产物。
- 只支持当前进程的活动媒体连接；连接缺失或发送失败不得返回成功 ACK。
- 不修改 `tests/core`；临时测试保留在 `tests/tdd/rendering-3d-delivery/`，用户可手动删除该目录。

---

### Task 1: 将真实 chatIndex 传给 3D 服务

**Files:**
- Modify: `tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py`
- Modify: `src/assistant_agent/media/image_to_3d.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_to_3d/tool.py`

**Interfaces:**
- Consumes: `ToolContext.metadata["request_metadata"]["agent_service"]["chat_index"]`。
- Produces: `ImageTo3DStarter.start(*, session_id: str, chat_index: str, src_image: str, output_format: str) -> ImageTo3DSubmission`。

- [x] **Step 1: 编写 adapter URL 和 Tool metadata 的失败测试**

  将 adapter 请求断言改为真实 `chat-sentinel`：

  ```python
  result = adapter.start(
      session_id="session-sentinel",
      chat_index="chat-sentinel",
      src_image="cake_001",
  )
  assert payload["cb_url"].endswith(
      "/session-sentinel/chat-sentinel/3d-gen-back"
  )
  ```

  在 Tool 测试中提供完整真实 metadata，并让 fake adapter 记录 `chat_index`：

  ```python
  ToolContext(
      session_id="session-sentinel",
      metadata={
          "request_metadata": {
              "agent_service": {"chat_index": "chat-sentinel"}
          }
      },
  )
  assert calls[0]["chat_index"] == "chat-sentinel"
  ```

- [x] **Step 2: 运行测试并确认 RED**

  Run:

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py -k 'adapter_reads_src_image_id or tool_uses_runtime_owned_identity'
  ```

  Expected: adapter 不接受 `chat_index`，或回调 URL 仍包含 `/0/`。

- [x] **Step 3: 实现最小 metadata 提取与 URL 构造**

  扩展协议和 adapter：

  ```python
  def start(
      self,
      *,
      session_id: str,
      chat_index: str,
      src_image: str,
      output_format: str = "mp4",
  ) -> ImageTo3DSubmission:
      callback_url = (
          f"{self.settings.public_base_url.rstrip('/')}/calling-agent-service/v1/"
          f"{urllib.parse.quote(session_id, safe='')}/"
          f"{urllib.parse.quote(chat_index, safe='')}/3d-gen-back"
      )
  ```

  Tool 从可信 request metadata 读取 chat index，非媒体入口保持兼容默认值 `0`：

  ```python
  request_metadata = context.metadata.get("request_metadata")
  agent_service = (
      request_metadata.get("agent_service")
      if isinstance(request_metadata, dict)
      else None
  )
  chat_index = agent_service.get("chat_index") if isinstance(agent_service, dict) else None
  normalized_chat_index = str(chat_index).strip() if chat_index is not None else "0"
  submission = self.adapter.start(
      session_id=context.session_id,
      chat_index=normalized_chat_index or "0",
      src_image=src_image,
      output_format="mp4",
  )
  ```

- [x] **Step 4: 更新测试 fake 签名并确认 GREEN**

  所有该文件中的 fake adapter `start` 增加 keyword-only `chat_index: str`，然后运行：

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py
  ```

  Expected: 当前 3D TDD 测试全部通过。

### Task 2: 建立活动媒体连接 registry

**Files:**
- Create: `src/assistant_agent/media/rendering_3d_relay.py`
- Modify: `tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`

**Interfaces:**
- Consumes: runtime session ID、connection ID、手机号和 `Callable[[dict[str, Any]], Awaitable[None]]`。
- Produces: `Rendering3DRelayRegistry.register`、`unregister`、`send(session_id, frame_factory)` 和 `get_rendering_3d_relay_registry()`。

- [x] **Step 1: 编写 registry 生命周期的失败测试**

  使用真实 registry 和一个仅收集结构化 frame 的 async sender，验证注册后可发送、旧连接不能注销新连接、注销当前连接后发送失败：

  ```python
  sent: list[dict[str, Any]] = []

  async def sender(frame: dict[str, Any]) -> None:
      sent.append(frame)

  registry = Rendering3DRelayRegistry()
  await registry.register(
      session_id="session-sentinel",
      connection_id="connection-1",
      number="13800138000",
      sender=sender,
  )
  binding = await registry.send(
      "session-sentinel",
      lambda active: {
          "message": "chatResponse",
          "body": json.dumps({"number": active.number}),
      },
  )
  assert binding.number == "13800138000"
  assert json.loads(sent[0]["body"])["number"] == "13800138000"
  ```

- [x] **Step 2: 运行 registry 测试并确认 RED**

  Run:

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py -k relay_registry
  ```

  Expected: `assistant_agent.media.rendering_3d_relay` 尚不存在。

- [x] **Step 3: 实现最小 registry**

  ```python
  RelaySender = Callable[[dict[str, Any]], Awaitable[None]]
  RelayFrameFactory = Callable[["Rendering3DRelayBinding"], dict[str, Any]]

  @dataclass(frozen=True)
  class Rendering3DRelayBinding:
      connection_id: str
      number: str
      sender: RelaySender

  class Rendering3DRelayRegistry:
      def __init__(self) -> None:
          self._bindings: dict[str, Rendering3DRelayBinding] = {}
          self._lock = asyncio.Lock()

      async def register(self, *, session_id: str, connection_id: str,
                         number: str, sender: RelaySender) -> None:
          async with self._lock:
              self._bindings[session_id] = Rendering3DRelayBinding(
                  connection_id=connection_id,
                  number=number,
                  sender=sender,
              )

      async def unregister(self, *, session_id: str, connection_id: str) -> None:
          async with self._lock:
              binding = self._bindings.get(session_id)
              if binding is not None and binding.connection_id == connection_id:
                  self._bindings.pop(session_id, None)

      async def send(
          self,
          session_id: str,
          frame_factory: RelayFrameFactory,
      ) -> Rendering3DRelayBinding:
          async with self._lock:
              binding = self._bindings.get(session_id)
          if binding is None:
              raise Rendering3DRelayUnavailable(session_id)
          await binding.sender(frame_factory(binding))
          return binding
  ```

  `send` 在锁内读取 binding、锁外执行 sender；未找到时抛出稳定的 `Rendering3DRelayUnavailable`。

- [x] **Step 4: 接入 WebSocket 注册与清理**

  在 chat 已解析、`userNumber` 已知后注册 sender closure：

  ```python
  async def sender(response: dict[str, Any]) -> None:
      await _send_response(websocket, response, state=state)

  await relay_registry.register(
      session_id=prepared.session_id,
      connection_id=state.connection_id,
      number=prepared.user_number,
      sender=sender,
  )
  ```

  在连接 `finally` 中使用相同 runtime session ID 和 connection ID 注销；closure 必须复用
  `_send_response`，从而复用 `send_lock`、closed 检查与发送统计。

- [x] **Step 5: 运行 registry 与既有媒体测试并确认 GREEN**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/rendering-3d-delivery tests/tdd/agent_service_image_delivery
  ```

  Expected: 两个临时 feature 目录全部通过。

### Task 3: 回调构造并投递完整 chatResponse

**Files:**
- Modify: `tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py`
- Modify: `src/assistant_agent/api/rendering_3d_callback.py`

**Interfaces:**
- Consumes: `Rendering3DRelayRegistry.send(session_id, frame_factory)` 和回调 `mediaType/mediaUrl/image`。
- Produces: `create_rendering_3d_callback_router(relay_registry: Rendering3DRelayRegistry | None = None)`；成功时先投递再 ACK。

- [x] **Step 1: 编写三种 mediaType 的失败测试**

  参数化 `ply/glb/mp4`，使用注入 registry 和真实 FastAPI route，解析发送 frame 的 `body` 后断言字面量：

  ```python
  @pytest.mark.parametrize(
      ("media_type", "media_url", "detail"),
      [
          ("ply", "http://renderer/model.ply", {"type": "TD_MODEL", "modelUrl": "http://renderer/model.ply"}),
          ("glb", "http://renderer/model.glb", {"type": "TD_MODEL", "modelUrl": "http://renderer/model.glb"}),
          ("mp4", "http://renderer/model.mp4", {"type": "VIDEO", "videoUrl": "http://renderer/model.mp4"}),
      ],
  )
  def test_3d_callback_relays_media_result(media_type, media_url, detail):
      response = client.post(
          "/calling-agent-service/v1/session-sentinel/chat-sentinel/3d-gen-back",
          json={"mediaType": media_type, "mediaUrl": media_url, "image": None},
      )
      assert response.status_code == 200
      body = json.loads(sent[0]["body"])
      assert body["message"]["content"]["intentResult"]["detail"] == [detail]
  ```

  断言 `message="chatResponse"`、body 中的手机号、两个 `chatIndex`、`ANSWER`、
  `display_only=false`、空 execution/web、`SUCCESS` 和单项 detail。

- [x] **Step 2: 编写连接缺失的失败测试**

  对空 registry POST 合法回调，断言非 2xx 且响应不包含成功 ACK；非法 `mediaType=image` 断言 422。

- [x] **Step 3: 运行回调测试并确认 RED**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py -k 'callback_relays or callback_without_active or callback_rejects_unsupported'
  ```

  Expected: route 仍只 ACK，未调用 registry，或不拒绝 `image`。

- [x] **Step 4: 实现 schema、frame builder 与投递错误**

  ```python
  class Rendering3DCallback(BaseModel):
      mediaType: Literal["ply", "glb", "mp4"]
      mediaUrl: HttpUrl
      image: str | None = None

  def _rendering_detail(media_type: str, media_url: str) -> dict[str, str]:
      if media_type == "mp4":
          return {"type": "VIDEO", "videoUrl": media_url}
      return {"type": "TD_MODEL", "modelUrl": media_url}
  ```

  先从 registry 取得手机号并构造完整 response。为避免“先取后发”的竞态，registry `send` 接受一个
  `Callable[[Rendering3DRelayBinding], dict[str, Any]]` frame factory，在同一个已解析 binding 上生成
  frame 并调用 sender；连接缺失转换成 HTTP 409，发送失败转换成 HTTP 503。只有 send 正常返回后
  生成成功 ACK。

- [x] **Step 5: 运行完整 3D TDD 测试并确认 GREEN**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/rendering-3d-delivery
  ```

  Expected: 全部通过，无 warning/error。

### Task 4: 同步权威文档并完成验证

**Files:**
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/superpowers/plans/2026-08-04-rendering-3d-callback-media-relay.md`

**Interfaces:**
- Consumes: 已实现的回调转发行为。
- Produces: 与代码一致的 Gateway 边界和 Media-Agent wire contract。

- [x] **Step 1: 更新 Gateway 边界**

  将“回调只确认、不转发”改为：回调 route 只负责校验、映射并经活动媒体 WebSocket 中继，不进入
  Gateway run/LLM；模型或视频仍不由 Agent 下载、存储或解析。

- [x] **Step 2: 更新 Media-Agent 协议**

  写明真实 `chatIndex` 回调 URL、`ply/glb/mp4` 映射表、完整 `chatResponse` 示例、active-session
  失败语义，以及 WebSocket sender/lock 的并发边界；删除“3D 服务通过 Agent 外渠道交付”的旧描述。

- [x] **Step 3: 执行最小功能验证**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/rendering-3d-delivery tests/tdd/agent_service_image_delivery
  ```

- [x] **Step 4: 执行静态差异检查**

  ```bash
  git diff --check -- src/assistant_agent/api/rendering_3d_callback.py src/assistant_agent/api/agent_service_websocket.py src/assistant_agent/media/image_to_3d.py src/assistant_agent/media/rendering_3d_relay.py src/assistant_agent/tools/plugins/builtin/image_to_3d/tool.py tests/tdd/rendering-3d-delivery/test_rendering_3d_delivery.py docs/gateway-architecture.md docs/media-agent-service-websocket.md docs/superpowers/specs/2026-08-04-rendering-3d-callback-media-relay-design.md docs/superpowers/plans/2026-08-04-rendering-3d-callback-media-relay.md
  ```

- [x] **Step 5: 汇报结果，不自动提交**

  汇报 `Core invariant: unchanged.`；说明更新了
  `tests/tdd/rendering-3d-delivery` 临时 RED/GREEN，用户可手动删除整个目录；列出实际测试命令和
  结果。由于当前为 Default mode 且用户未要求提交，不创建 commit、push 或 PR。
