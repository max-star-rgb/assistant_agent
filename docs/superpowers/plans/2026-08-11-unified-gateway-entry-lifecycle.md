# Gateway 与产品入口统一生命周期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Web UI/HTTP CLI 通过 `/agent/run` 获得 JSON 或 SSE token stream，让手机链路继续通过 `/agent-service/v1`，并使两种传输复用同一 Gateway run lifecycle、terminal response 和 citation contract。

**Architecture:** `GatewayTurnFacade` 增加可迭代的 `GatewayTurnStream`，`run_turn()` 退化为消费该 stream 的兼容 facade。HTTP adapter 只做 content negotiation、SSE 映射和 delivery cancel；Media adapter 只做 vendor capability/projection。`agent_cli.py` 与 `media_simulator.py` 是职责互斥的两个客户端入口。

**Tech Stack:** Python 3.12、FastAPI/Starlette `StreamingResponse`、Pydantic v2、stdlib `urllib`、pytest、PyCharm `.run` XML。

## Global Constraints

- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`；pytest 不调用真实 Provider。
- 不新增第三方依赖，不引入第二套 Agent loop、session store 或 run state machine。
- `/agent/run` 未请求 SSE 时保持现有 JSON `AgentRunResponse`。
- `/agent-service/v1` 未声明 `urlCitationAnnotationsV1` 时保持旧 wire shape。
- `run_client.py` 直接迁移为 `media_simulator.py`，不保留旧 shim。
- `/ws/gateway` 保留为内部 canonical Gateway 协议与调试入口。
- 保留工作区既有改动；当前分支已有重叠未提交工作，本轮不自动 commit。

---

### Task 1: GatewayTurnStream 统一流式 facade

**Files:**
- Modify: `src/assistant_agent/gateway/turn_facade.py`
- Test: `tests/tdd/unified-gateway-entry-lifecycle/test_gateway_turn_stream.py`

**Interfaces:**
- Consumes: `GatewayTurnRequest`、`GatewaySessionManager.acquire()`、既有 Gateway `Frame`。
- Produces: `GatewayTurnFacade.start_turn(request) -> GatewayTurnStream`；stream 暴露 `correlation`、异步 frame iterator、`result()`、`cancel()`、`aclose()`。
- Preserves: `GatewayTurnFacade.run_turn()` 的返回值、timeout、consumer failure 与 caller cancellation 语义。

- [ ] **Step 1: 写 GatewayTurnStream RED 测试**

```python
async def scenario() -> None:
    facade = GatewayTurnFacade(manager=manager)
    stream = await facade.start_turn(GatewayTurnRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="query-sentinel",
    ))
    assert stream.correlation.run_id.startswith("run_")
    frames = [frame async for frame in stream]
    result = await stream.result()
    assert [item["type"] for item in frames] == [
        "run.started",
        "stream.chunk",
        "run.end",
    ]
    assert result.response_text == "answer-sentinel"
```

补充同文件用例：`await stream.cancel(source="http", reason="client_cancelled")` 只向匹配 run 发送 `run.cancel`；`aclose()` 在未终态时 best-effort cancel；`run_turn(on_stream_chunk=...)` 兼容回归。

- [ ] **Step 2: 运行确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle/test_gateway_turn_stream.py`

Expected: FAIL，`GatewayTurnFacade` 没有 `start_turn`。

- [ ] **Step 3: 实现最小 GatewayTurnStream**

```python
class GatewayTurnStream:
    @property
    def correlation(self) -> GatewayTurnCorrelation:
        return self._correlation

    def __aiter__(self) -> GatewayTurnStream:
        return self

    async def __anext__(self) -> Frame:
        item = await self._public_frames.get()
        if item is _STREAM_END:
            raise StopAsyncIteration
        return item

    async def result(self) -> GatewayTurnResult:
        return await self._result_future

    async def cancel(self, *, source: str, reason: str) -> None:
        await _best_effort_cancel(
            self._endpoint,
            session_id=self._session_id,
            turn_id=self._correlation.turn_id,
            run_id=self._correlation.run_id,
            source=source,
            reason=reason,
        )

    async def aclose(self) -> None:
        if not self._result_future.done():
            await self.cancel(source=self._cancel_source, reason=self._cancel_reason)
        await self._finish()

class GatewayTurnFacade:
    async def start_turn(self, request: GatewayTurnRequest) -> GatewayTurnStream:
        handle, dispatcher, inbox, correlation = await self._prepare_turn(request)
        return GatewayTurnStream.start(
            request=request,
            endpoint=handle.endpoint,
            dispatcher=dispatcher,
            inbox=inbox,
            correlation=correlation,
        )
```

实现使用 facade 已有 dispatcher/inbox；stream 是唯一 inbox consumer，累积 chunks/frames 并以 `run.end` 形成 `GatewayTurnResult`。timeout、endpoint close 和 error frame 设置 result exception；任何终态都 unregister。`run_turn()` 调用 `start_turn()`，遍历 frame 并仅对 `stream.chunk` 调用兼容 callback。

- [ ] **Step 4: 运行确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle/test_gateway_turn_stream.py`

Expected: PASS。

### Task 2: `/agent/run` SSE、terminal 与 HTTP cancel

**Files:**
- Create: `src/assistant_agent/api/agent_sse.py`
- Modify: `src/assistant_agent/api/models.py`
- Modify: `src/assistant_agent/api/routes_agent.py`
- Modify: `src/assistant_agent/api/gateway_runtime.py`
- Test: `tests/tdd/unified-gateway-entry-lifecycle/test_agent_run_sse.py`

**Interfaces:**
- Consumes: `GatewayTurnStream`、HTTP capture `AgentRunResponse`、`AuthContext` identity policy。
- Produces: `POST /agent/run` content negotiation；`POST /agent/runs/{run_id}/cancel`；`AgentRunCancelRequest` / `AgentRunCancelResponse`。
- SSE mapping: `run.started`、`response.delta`、`run.progress`、`tool.event`、`response.completed`、`run.failed`、`run.cancelled`。

- [ ] **Step 1: 写 SSE/JSON/cancel RED 测试**

```python
def test_agent_run_sse_streams_delta_then_complete(test_client) -> None:
    with test_client.stream(
        "POST",
        "/agent/run",
        headers={"Accept": "text/event-stream"},
        json={
            "user_id": "user-sentinel",
            "session_id": "session-sentinel",
            "text": "query-sentinel",
        },
    ) as response:
        body = "".join(response.iter_text())
    events = parse_sse_events(body)
    assert [item.event for item in events] == [
        "run.started",
        "response.delta",
        "response.completed",
    ]
    assert events[-1].data["response_text"] == "answer-sentinel"
    assert events[-1].data["annotations"][0]["source_id"] == "source_1"
```

同文件断言 `Accept: application/json` 保持 JSON；started 后失败产生 terminal SSE；cancel endpoint 对 identity 不匹配返回 404/403 且不取消别人的 run；stream generator 被取消时调用 `GatewayTurnStream.aclose()`。

- [ ] **Step 2: 运行确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle/test_agent_run_sse.py`

Expected: FAIL，`/agent/run` 仍只返回 JSON且 cancel route 不存在。

- [ ] **Step 3: 实现 SSE encoder 与 HTTP delivery registry**

```python
class ServerSentEvent(BaseModel):
    event: str
    data: dict[str, Any]

def encode_sse(event: ServerSentEvent) -> bytes:
    payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.event}\ndata: {payload}\n\n".encode("utf-8")

class AgentRunCancelRequest(BaseModel):
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)

class AgentRunCancelResponse(BaseModel):
    run_id: str
    status: Literal["cancel_requested"]
```

`gateway_runtime` 增加 bounded、identity-bound 的 HTTP active stream registry，只保存活动 `GatewayTurnStream` handle，不保存第二份 run 状态。SSE generator 从 stream frame 机械映射；完成时 pop capture 并用完整 `AgentRunResponse` 发 terminal。响应 headers 至少包含 `Cache-Control: no-cache`、`X-Accel-Buffering: no`、`Content-Type: text/event-stream; charset=utf-8`。

- [ ] **Step 4: 运行确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle/test_agent_run_sse.py`

Expected: PASS。

### Task 3: HTTP/SSE Agent CLI

**Files:**
- Create: `src/assistant_agent/clients/__init__.py`
- Create: `src/assistant_agent/clients/http_agent.py`
- Create: `scripts/agent_cli.py`
- Test: `tests/tdd/unified-gateway-entry-lifecycle/test_http_agent_client.py`

**Interfaces:**
- Consumes: `/agent/run` JSON/SSE contract 和 `/agent/runs/{run_id}/cancel`。
- Produces: `HttpAgentClient.run_stream(request) -> Iterator[AgentClientEvent]`、`run_json(request) -> dict[str, Any]`、`cancel(run_id, user_id, session_id)`；thin interactive CLI。

- [ ] **Step 1: 写 HTTP client RED 测试**

```python
def test_http_agent_client_parses_sse_and_terminal_annotations(fake_server) -> None:
    client = HttpAgentClient(server=fake_server.base_url)
    events = list(client.run_stream({
        "user_id": "user-sentinel",
        "session_id": "session-sentinel",
        "text": "query-sentinel",
    }))
    assert [item.event for item in events] == [
        "run.started",
        "response.delta",
        "response.completed",
    ]
    assert events[-1].data["annotations"][0]["url"] == "https://example.com/1"
```

同文件覆盖 JSON mode、非 2xx 结构化错误、UTF-8 多行 chunk、cancel request body；fake server 只监听临时 loopback，不访问公网。

- [ ] **Step 2: 运行确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle/test_http_agent_client.py`

Expected: FAIL，`assistant_agent.clients.http_agent` 尚不存在。

- [ ] **Step 3: 实现 stdlib HTTP/SSE client 与 thin CLI**

```python
class AgentClientEvent(BaseModel):
    event: str
    data: dict[str, Any]

class HttpAgentClient:
    def run_stream(self, request: Mapping[str, Any]) -> Iterator[AgentClientEvent]:
        http_request = self._json_request(
            "/agent/run",
            request,
            accept="text/event-stream",
        )
        with urlopen(http_request, timeout=self.timeout_s) as response:
            yield from parse_sse_response(response)

    def run_json(self, request: Mapping[str, Any]) -> dict[str, Any]:
        http_request = self._json_request(
            "/agent/run",
            request,
            accept="application/json",
        )
        with urlopen(http_request, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def cancel(self, *, run_id: str, user_id: str, session_id: str) -> dict[str, Any]:
        http_request = self._json_request(
            f"/agent/runs/{quote(run_id, safe='')}/cancel",
            {"user_id": user_id, "session_id": session_id},
            accept="application/json",
        )
        with urlopen(http_request, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
```

客户端使用 `urllib.request`，不新增依赖。CLI 默认 streaming，逐个 `response.delta` 打印；terminal 只打印正文尚未流出的剩余部分，并把 annotations 以紧凑 `来源 [n] 标题 URL` 诊断输出。Ctrl-C 在已知 run_id 时调用 cancel；`--no-stream` 走 JSON。

- [ ] **Step 4: 运行确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle/test_http_agent_client.py`

Expected: PASS。

### Task 4: Media citation capability 与终态投影

**Files:**
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Test: `tests/tdd/unified-gateway-entry-lifecycle/test_media_citation_capability.py`

**Interfaces:**
- Consumes: `GatewayTurnResult.payload.annotations`。
- Produces: negotiated `clientCapabilities.urlCitationAnnotationsV1`；成功 terminal `intentResult.annotations`。
- Preserves: 未协商客户端、PROCESSING packet、failure packet、图片 legacy response、ACK/progress 行为。

- [ ] **Step 1: 写 capability RED 测试**

```python
def test_media_terminal_projects_annotations_only_when_negotiated() -> None:
    negotiated = _prepared_chat_response(
        prepared,
        state=state_with_citation_capability,
        turn=completed_turn_with_annotations,
        delivery=delivery,
        sequence=1,
    )
    legacy = _prepared_chat_response(
        prepared,
        state=legacy_state,
        turn=completed_turn_with_annotations,
        delivery=delivery,
        sequence=1,
    )
    assert parse_body(negotiated)["message"]["content"]["intentResult"]["annotations"][0]["source_id"] == "source_1"
    assert "annotations" not in parse_body(legacy)["message"]["content"]["intentResult"]
```

补充断言 unsafe/extra fields 不能从通用 metadata 注入，failure/processing 不发送 annotations。

- [ ] **Step 2: 运行确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle/test_media_citation_capability.py`

Expected: FAIL，capability allowlist 和 terminal projection 尚不存在。

- [ ] **Step 3: 最小实现 capability/projection**

`_delivery_capabilities()` allowlist 增加 `urlCitationAnnotationsV1`；新增纯函数从 `turn.payload["annotations"]` 重新用 `UrlCitationAnnotation.model_validate()` 校验并 bounded serialize。只在 completed、非图片 terminal 且 capability 为真时合并到 `intent_result`。

- [ ] **Step 4: 运行确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle/test_media_citation_capability.py`

Expected: PASS。

### Task 5: `run_client` 迁移为 `media_simulator`

**Files:**
- Rename: `scripts/run_client.py` -> `scripts/media_simulator.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `src/assistant_agent/observability/otel_mapping.py`
- Modify: `src/assistant_agent/observability/turn_summary.py`
- Modify: repository tests importing/loading `scripts/run_client.py`
- Test: `tests/tdd/unified-gateway-entry-lifecycle/test_media_simulator_identity.py`

**Interfaces:**
- Consumes: existing Media-Agent console behavior和 `urlCitationAnnotationsV1`。
- Produces: `clientInfo.clientType=media_simulator`、`media-simulator-*` session id、`--citations` flag；移除 `scripts/run_client.py`。

- [ ] **Step 1: 写重命名 RED 测试**

```python
def test_media_simulator_uses_unambiguous_identity() -> None:
    body = assistant_control_body(
        user_number="user-sentinel",
        call_type="CHAT",
        model_name=None,
        chat_progress=True,
        chat_response_ack=True,
        citations=True,
    )
    assert body["clientInfo"]["clientType"] == "media_simulator"
    assert body["clientCapabilities"]["urlCitationAnnotationsV1"] is True
    assert new_media_session_id().startswith("media-simulator-")
```

另断言仓库不存在 `scripts/run_client.py`，server prompt-safe classification 输出 `media_simulator`。

- [ ] **Step 2: 运行确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle/test_media_simulator_identity.py`

Expected: FAIL，脚本仍名为 `run_client.py` 且 client type 为 `run_client`。

- [ ] **Step 3: 迁移脚本与所有仓库内调用点**

保留现有交互、Workflow tail、重连、图片上限和 ACK 行为；只改变职责名称并增加 citation terminal 解析。更新 server allowlist/normalization，使历史 `run_client` trace 可读但新运行只产生 `media_simulator`。测试文件名不要求机械改名，但所有脚本路径/import 必须指向新文件。

- [ ] **Step 4: 运行确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle/test_media_simulator_identity.py tests/tdd/deep-research-mode/test_run_client_restart_recovery.py`

Expected: PASS；旧测试可保留历史文件名但加载新脚本。

### Task 6: PyCharm 配置、authority 与跨入口收敛

**Files:**
- Delete: `.run/Assistant Client.run.xml`
- Create: `.run/Agent CLI.run.xml`
- Create: `.run/Media Simulator.run.xml`
- Modify: `scripts/README.md`
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/authority.toml`
- Test: `tests/tdd/unified-gateway-entry-lifecycle/test_pycharm_run_configs.py`

**Interfaces:**
- Consumes: `scripts/agent_cli.py`、`scripts/media_simulator.py`。
- Produces: PyCharm shared run configs与更新后的 authority routing。

- [ ] **Step 1: 写配置 RED 测试**

```python
def test_pycharm_client_configs_target_distinct_transports() -> None:
    agent = parse_run_config(".run/Agent CLI.run.xml")
    media = parse_run_config(".run/Media Simulator.run.xml")
    assert agent.script_name.endswith("scripts/agent_cli.py")
    assert "--interactive" in agent.parameters
    assert media.script_name.endswith("scripts/media_simulator.py")
    assert "--chat-response-ack" in media.parameters
    assert "--citations" in media.parameters
```

同时断言两份 XML 使用 `hello_agent`、`$PROJECT_DIR$`，不包含 key/token，旧 `Assistant Client` 配置不存在。

- [ ] **Step 2: 运行确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle/test_pycharm_run_configs.py`

Expected: FAIL，新配置不存在且旧配置仍存在。

- [ ] **Step 3: 创建 PyCharm 配置并同步 authority/docs**

`Agent CLI` 参数固定为 `--server http://127.0.0.1:8089 --interactive`；`Media Simulator` 参数固定为 `--server http://127.0.0.1:8089 --stream --chat-progress --chat-response-ack --citations --interactive`。两者继承环境且不内嵌凭据。authority 把原 `scripts/run_client.py` source glob 替换为两个明确 client script，并记录 `/agent/run` SSE 与 Media capability 的 owner。

- [ ] **Step 4: 运行完整定向验证**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-gateway-entry-lifecycle tests/tdd/url-citation-delivery tests/tdd/provider-search-provenance tests/tdd/deep-research-mode/test_run_client_restart_recovery.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent scripts
git diff --check
```

Expected: 全部 exit 0；pytest 不访问真实 Provider。
