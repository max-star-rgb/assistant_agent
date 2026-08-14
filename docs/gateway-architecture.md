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

不注册旧 graph alias，因此旧 thread/checkpoint 不会被新图解释。Agent Server 原生拥有 assistant、thread、
run、queue、checkpoint、interrupt/resume、cancel、stream 和 Store。项目不再在生产入口维护另一份 run manager、
cancel token、checkpoint facade 或产品状态机。

公开 Graph 输入为严格 `AssistantRootInput`：

```json
{
  "messages": [{"role": "user", "content": "hello"}],
  "execution_mode": "fast"
}
```

`execution_mode` 只允许 `fast|planning`。可信 `user_id`、`tenant_id`、`entry_profile` 与
`media_capabilities` 位于 `AgentServerRunContext`，由认证 principal 校验；模式不放入 context。

## Composition 与资源

factory lifespan 创建 `AgentServerExecutionOwner`。它只持有：

- 标准 `BaseChatModel` Provider adapter；
- 静态本地 `BaseTool` 与官方 MCP tools；
- 一个 `MemoryBackend`；
- 已编译但不绑定 saver/Store 的 `AssistantRootGraph`；
- 对应 close targets。

它不构造 `AgentGraphRuntime`、`AssistantTurnState`、`ToolExecutor`、`ProductEventProjector` 或
`WorkflowGraphHost`。LangMem composition 可以引用 Server 注入的同一个 Store，但 compiled graph 本身不显式
绑定本地 saver/store。

## Auth 与身份

mock/local 模式可用 `X-Assistant-User`、`X-Assistant-Tenant` 构造开发 principal。real mode 要求
`ASSISTANT_AGENT_SERVER_SERVICE_TOKEN`，媒体服务还需对 `<user>\n<tenant>` 做 HMAC-SHA256 签名。
thread metadata 按 auth owner 限制；run 与 Store 沿用同一 principal。connection、vendor session、thread、run
与 delivery ID 始终是不同身份轴。

## `/agent-service/v1`

custom route 只解析 vendor envelope、关联媒体连接与 native thread/run、使用公开 `langgraph_sdk` 创建/消费/
取消 run，并把最新标准 `AIMessage` 投影为 `chatResponse`。Graph 输入是标准多模态 messages 与结构化 mode；
adapter 不读取 checkpoint、不执行 Tool/Memory、不翻译项目错误码。

SDK stream 使用 messages/updates/values 和 resumable run。短暂传输断开后按 last event ID 调用
`threads.join_stream`；WebSocket 断开时 best-effort cancel 当前连接仍活动的 native runs。delivery ACK 不改变
run 或 checkpoint。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_gateway_contract.py \
  tests/tdd/native-agent-parent-graph/test_agent_server_factory.py \
  tests/tdd/native-agent-parent-graph/test_media_native_adapter.py
```
