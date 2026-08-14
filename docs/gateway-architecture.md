# Agent Server 部署架构

Last updated: 2026-08-14

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant Graph 的 Agent Server 部署、身份和资源生命周期权威 |
| Owns | auth、assistant、thread、run、queue、checkpoint、Store、cancel、stream 与 custom route 装配 |
| Does not own | Assistant 节点内部推理、Tool 治理、Memory backend 语义、Media-Agent wire 字段 |
| 源码与 schema 入口 | `langgraph.json`、`src/assistant_agent/agent_server/`、`scripts/run_server.py` |
| 验证入口 | `docs/authority.toml` 中 `agent-server.verification` |
| 相邻 authority | 媒体 wire 见 `media-agent-service-websocket.md`；节点流见 `runtime-event-stream-architecture.md` |

## 唯一生产执行边界

生产环境只有一套 Graph server：LangGraph Agent Server。仓库不再维护 FastAPI 包裹 Gateway、Gateway
包裹 Runtime 的平行执行链，也不在入口进程中自行拥有 session、run queue 或 checkpointer。

```text
LangGraph public API / SDK clients
              |
              +-- auth -> assistant / thread / run / stream / cancel / checkpoint / Store
              |
Media service +-- /agent-service/v1 custom route
                           |
                           +-- wire parse/project
                           +-- public SDK -> native thread/run/cancel
                                      |
                                      v
                            deployed Assistant StateGraph
```

`langgraph.json` 是唯一生产 serving manifest。`scripts/run_server.py` 只是 `langgraph dev` 的本地启动
包装；`scripts/agent_cli.py` 只通过公开 `langgraph_sdk` 访问部署。

## 资源心智模型

| 资源 | 权威 owner | 含义 |
| --- | --- | --- |
| auth principal | Agent Server auth middleware | 调用方身份与 delegation 权限 |
| assistant | Agent Server | 指向部署的 Graph 定义 |
| thread | Agent Server | 多轮会话与 checkpoint 轴 |
| run | Agent Server | thread 上的一次执行，可排队、流式读取和取消 |
| checkpoint | Agent Server/checkpointer | Graph State 的持久执行快照 |
| Store | Agent Server | 可选跨 thread namespace 数据资源，供 LangMem 等后端使用 |
| media connection | custom route | 一次 WebSocket 传输连接，不是 thread |
| delivery ID | custom route | 媒体响应 ACK 关联，不是 run 或 checkpoint |

同一 conversation 复用 `thread_id`；每次输入创建新 run。连接断开不应被解释成删除 thread 或 Store。
并发策略由 run API 的 `multitask_strategy` 指定；当前媒体入口使用 `enqueue`。取消通过原生 run cancel，
不再维护第二份 cancellation token/session manager。

## Graph 装配

Agent Server 调用 `assistant_agent.agent_server.graph:assistant_graph` factory，并提供受认证
`ServerRuntime`、checkpointer 和可选 Store。factory 创建本次 worker service，编译不绑定本地 saver/store
的 graph，并在 factory lifespan 结束时关闭 Provider/Memory 资源。

Graph 输入只含 `request_input`；可信身份、tenant、mode、entry profile 和 media capability 位于严格
`AgentServerRunContext`。factory 必须用认证 principal 校验 context delegation。custom route 内部若要发起
run，必须经公开同源 API 并转发 Authorization；不得用 `/noauth` 后再相信客户端可伪造的 context。
worker composition 根据受信配置构造不可变 `GraphExecutionPolicy` 并放入 LangGraph Runtime context；
Assistant checkpoint 只保存对应 digest 与已用计数，不复制模型或 Tool 最大调用数。

mock/local 模式可用 `X-Assistant-User`、`X-Assistant-Tenant` 构造多个开发 principal。real mode 先验证
`ASSISTANT_AGENT_SERVER_SERVICE_TOKEN`，再要求媒体服务提供 `X-Assistant-User`、
`X-Assistant-Tenant` 与 `X-Assistant-Signature`；签名是服务 secret 对
`<user>\n<tenant>` 的 HMAC-SHA256。认证结果的 identity 是终端 user，不是固定媒体服务账号；thread、run
和 Store 均按该 identity 授权。裸 `userNumber` 或未签名 header 不能形成生产 principal。

## custom route 边界

`/agent-service/v1` 保留媒体侧 envelope。适配器只允许：

- 解析/校验 vendor frame；
- 关联 connection、vendor session、thread、run、chat 和 delivery；
- 调用公开 SDK 创建 thread/run、消费 stream、取消 run；
- 将原生终态机械投影为媒体响应并处理 ACK；
- 承载不执行 Graph 的 callback route。

它不得构造 `AgentGraphRuntime`，不得实现排队、checkpoint、长期记忆策略或 Tool 执行。Graph State 也不放
WebSocket、SDK client、Provider client 或回调对象。

H.264 解码与 3D callback 属于媒体边缘资源。解码后的有界 JPEG 引用保存在 SQLite frame index，Graph
State 只携带 `video_id`；视觉 Tool 在 worker 中按引用读取。该本地实现要求 API/worker 共享受控数据卷，
多主机部署应把同一协议替换为对象存储 URI + 共享索引。3D callback 只向当前在线连接发布中性 artifact
事件，不启动第二次 Graph，也不宣称跨进程或离线可靠投递。

## 本地运行与生产边界

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py --port 8000
```

`langgraph dev` 的 in-memory runtime 只用于开发验证。生产部署使用 LangSmith Deployment/Agent Server
支持的持久资源和认证配置；不能把本地 in-memory 的重启行为当作生产 durability 证明。
