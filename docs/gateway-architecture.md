# Agent Server 部署架构

最后更新：2026-08-14

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant Graph 的 Agent Server 部署、身份和资源生命周期权威 |
| Owns | auth、assistant、thread、run、queue、checkpoint、Store、cancel、stream 与 custom route 装配 |
| Does not own | Assistant 节点推理、Tool schema、Memory 后端语义、Media-Agent wire 字段 |
| 源码与 schema 入口 | `langgraph.json`、`src/assistant_agent/agent_server/` |
| 验证入口 | `docs/authority.toml` 中 `agent-server.verification` |
| 相邻 authority | 媒体 wire 见 [`media-agent-service-websocket.md`](media-agent-service-websocket.md)；运行图见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md) |

## 唯一生产入口

`langgraph.json` 只注册：

```text
assistant-native-v1 -> assistant_agent.agent_server.graph:native_assistant_graph
```

不注册旧 graph alias，因此新图不解释旧 thread/checkpoint。Agent Server 原生拥有 assistant、thread、run、
queue、checkpoint、interrupt/resume、cancel、stream 和 LangGraph Store。项目不再在生产入口维护第二份 run
manager、cancel token、checkpoint facade 或产品状态机。

公开 Graph 输入为严格 `AssistantRootInput`：

```json
{
  "messages": [{"role": "user", "content": "hello"}],
  "execution_mode": "fast"
}
```

`execution_mode` 只允许 `fast|planning`。可信 `user_id`、`tenant_id`、`entry_profile` 与
`media_capabilities` 位于 `AgentServerRunContext`，由认证 principal 校验；执行模式不放入 context。

## 资源模型与 composition

| 资源 | 权威 owner | 含义 |
| --- | --- | --- |
| auth principal | Agent Server auth middleware | 调用方身份与 delegation 权限 |
| assistant/thread/run/checkpoint | Agent Server | Graph 定义、多轮状态与一次执行 |
| LangGraph Store | Agent Server | 可选跨 thread 数据资源，供 LangMem 等后端使用 |
| media connection | custom route | 一次 WebSocket 传输连接，不是 thread |
| delivery ID | custom route/outbox | 媒体 ACK 关联，不是 run 或 checkpoint |
| proactive delivery Store | Graph composition + custom route | 原生节点幂等入队，媒体连接 presence/claim/ACK；不是 LangGraph Store |

factory lifespan 创建 `AgentServerExecutionOwner`，持有标准 `BaseChatModel` Provider adapter、静态本地
`BaseTool` 与官方 MCP tools、一个 `MemoryBackend`、独立 `ProactiveDeliveryStore`、已编译但不绑定
checkpointer 的 `AssistantRootGraph`，以及对应 close targets。LangMem 可引用 Server 注入的 Store；主动投递
Store 则作为原生 `delivery_dispatch` 节点的 closure 资源，不进入 Graph State 或 Runtime context。

composition 只构造标准模型、Tool、Memory backend、主动投递 Store 与 `AssistantRootGraph`，不构造平行 Graph
Runtime、产品状态投影器或 Workflow host。

## Auth 与身份

mock/local 模式可用 `X-Assistant-User`、`X-Assistant-Tenant` 构造开发 principal。real mode 要求
`ASSISTANT_AGENT_SERVER_SERVICE_TOKEN`，媒体服务还需对 `<user>\n<tenant>` 做 HMAC-SHA256 签名。
thread metadata 按 auth owner 限制；run 与 Store 沿用同一 principal。connection、vendor session、thread、run
与 delivery ID 始终是不同身份轴。

## `/agent-service/v1`

custom route 只负责：

- 解析、校验 vendor frame；
- 关联 connection、vendor session、native thread/run、chat 与 delivery；
- 使用公开 `langgraph_sdk` 创建 run、消费 resumable stream、join 与 cancel；
- 从 terminal values 选择最新标准 `AIMessage` 并机械投影媒体响应；
- 按 native thread 从主动投递 Store 串行 claim，处理 ACK、lease 与重连补投；
- 承载不执行 Graph 的 callback route。

它不读取 checkpoint，不执行 Tool/Memory，不构造旧 Runtime，也不翻译项目 run/error 状态机。SDK stream 使用
messages/updates/values；短暂订阅断开后按 last event ID 调用 `threads.join_stream`。WebSocket 断开时
best-effort cancel 当前连接仍活动的 reactive runs；delivery ACK 不改变 run 或 checkpoint。

主动投递不引入全局 dispatcher 或第二套 Runtime。父 `AssistantRootGraph` 在 fast/planning 汇流后检查严格
`pending_deliveries` channel；非空时进入固定 `delivery_dispatch` 节点，用 `Runtime.execution_info` 的 native
thread/run 和认证 context 的 user 形成 envelope，并按稳定 message ID 幂等写入 Store，随后进入
`memory_commit`。媒体连接启动 thread-specific pull pump；durable 行只有匹配 ACK 才完成，断线或超时释放为
queued，ephemeral 离线时直接 skipped。当前 SQLite 实现面向单实例或共享受控卷，不宣称多主机一致性。

H.264 解码与 3D callback 属于媒体边缘资源。解码后的有界 JPEG 引用保存在 SQLite frame index，Graph State
只携带稳定引用；3D callback 只向当前在线连接发布中性 artifact，不启动第二次 Graph。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_gateway_contract.py \
  tests/tdd/native-agent-parent-graph/test_agent_server_factory.py \
  tests/tdd/native-agent-parent-graph/test_media_native_adapter.py \
  tests/tdd/native-proactive-delivery/test_native_dispatch.py \
  tests/tdd/native-proactive-delivery/test_media_delivery_pump.py
```
