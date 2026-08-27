# Agent Communication Routing

最后更新：2026-08-28

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Multi-agent instance routing、delegation 与 A2A adapter 的当前权威 |
| Owns | AgentDirectory、routing/delegation policy、transport、control plane、A2A JSON-RPC 与隔离边界 |
| Does not own | 生产父图、Agent Server/media wire、Tool 执行、Memory/context 策略 |
| 源码与 schema 入口 | `src/assistant_agent/multi_agent/`；当前无生产 A2A HTTP route |
| 验证入口 | `docs/authority.toml` 中 `agent-communication.verification` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Agent Server 见 [`agent-server-architecture.md`](agent-server-architecture.md) |

## 当前边界

生产用户会话路径只有 Agent Server 托管的 `AssistantRootGraph`。统一 `AssistantAgent` 静态装配 Deep Agents
`AsyncSubAgentMiddleware` 的 model-callable 后台 delegation Tool，并只允许启动注册的只读
`assistant-worker-v2` 兄弟图；它不经过关键词 router，也不启用本目录的 transport runtime。multi-agent 包仍是可选协议与
路由组件，不在 production custom route 中自动启用。

`AgentRouter` 只选择调用方显式注入的本地 invoker，并要求其返回 `AgentRunResponse`。仓库不再提供自动构造本地
controller/worker Runtime 的 convenience factory。`LocalAgentTransport` 同样只接受显式注入的 invoker；远端
`A2AJsonRpcTransport` 保持默认关闭、显式 allowlist、HTTPS/local opt-in、超时、响应大小和 circuit breaker 边界。

```text
显式本地实验                    远端 A2A pilot
AgentRouter                     AgentCommunicationService
  -> AgentDirectory               -> AgentDirectory
  -> injected invoker              -> A2AJsonRpcTransport
  -> AgentRunResponse               -> allowlisted remote endpoint
```

## 核心规则

- `AgentDirectory` 只登记受信 agent ID、capability、transport 与 endpoint metadata；不做动态发现。
- routing priority 只使用显式 target、受信 routing table、唯一 capability match 与 controller fallback。
- `AgentDelegationPolicy` 在 transport 前校验 source permission、allowed target、depth、timeout、budget 与循环。
- 跨 agent task 携带 user/session、parent run/trace、correlation、timeout 与 delegation depth，不能携带原始 Memory、
  Provider payload、secret、inline media body 或完整父会话。
- A2A Agent Card 只验证显式配置的远端，不自动注册或启用 agent。
- 远端失败必须返回结构化 `AgentTaskResult(status="failed")`，不能静默回退本地/mock 成功。
- control-plane store 只保存脱敏 route/audit/readiness 事实；JSONL durability 需要调用方显式提供路径。
- 当前无 Agent Server multi-agent custom route；重新暴露 HTTP/A2A 前必须证明它不会绕过主 Graph。

## 模块职责

| module | responsibility |
| --- | --- |
| `models.py` / `router_models.py` | message、task、artifact、session、route 与公开 metadata DTO |
| `agent_directory.py` | agent/capability/transport 静态目录 |
| `agent_routing_policy.py` | 确定性初始路由 |
| `agent_delegation_policy.py` | delegation allowlist、depth、loop、timeout 与 budget policy |
| `agent_delegation_context.py` | child-safe context 与 artifact summary |
| `agent_transports.py` | injected local invoker 与显式远端 A2A transport |
| `agent_communication.py` | policy 后的 task transport service |
| `agent_router.py` | 显式本地 invoker 选择与 `AgentRunResponse` metadata 投影 |
| `agent_control_plane.py` | 脱敏 run/audit/readiness store |
| `a2a_protocol.py` / `a2a_adapter.py` | A2A schema 与薄映射；当前未注册生产 route |

## 验证

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/multi_agent
```
