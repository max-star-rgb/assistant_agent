# Gateway 与产品入口统一生命周期设计

## 1. 目标

把当前多路入口收敛为“一套 Agent 运行生命周期、两种产品传输、三个明确客户端角色”：

- Web UI 与真正的 CLI 共用 HTTP `/agent/run`；普通请求返回 JSON，流式请求返回 SSE token stream。
- 手机 App 通过媒体服务连接 `/agent-service/v1`，继续使用 Media-Agent WebSocket 协议。
- `scripts/media_simulator.py` 只模拟手机 App/媒体服务协议；`scripts/agent_cli.py` 才是 HTTP/SSE 产品 CLI。
- `/ws/gateway` 保留为内部规范化 Gateway 协议、调试和集成入口，不再作为第四种产品入口扩张。
- 所有入口复用现有 `GatewaySessionManager -> GatewayRuntimeAdapter -> Assistant Runtime`，不得复制 Agent loop、session/run 状态机或终态合成逻辑。

## 2. 非目标

- 不把 HTTP/SSE 与 Media-Agent WebSocket 强行合并为同一个 URL 或 wire schema。
- 不让 `chatProgress`、`chatResponseAck`、WebSocket connection 等交付事实成为核心 Agent run 状态。
- 不引入新的前端框架，不实现 Web UI 页面。
- 不改变 Tool、Provider、Memory 或 Workflow 的治理边界。
- 不在测试中调用真实 Provider。

## 3. 产品入口与命名

```text
Web UI ───────┐
              ├── HTTP /agent/run ── JSON 或 SSE ──┐
Agent CLI ────┘                                    │
                                                   ▼
                                         Unified Gateway Lifecycle
                                                   ▲
手机 App ── 媒体服务 ── WS /agent-service/v1 ──────┘
                           ▲
                           │
                    Media Simulator
```

| 角色 | 稳定名称 | 入口 | 定位 |
| --- | --- | --- | --- |
| Web 产品客户端 | Web UI | `/agent/run` | HTTP JSON/SSE consumer |
| 命令行产品客户端 | `scripts/agent_cli.py` | `/agent/run` | 与 Web UI 共享协议 |
| 手机链路协议模拟器 | `scripts/media_simulator.py` | `/agent-service/v1` | 模拟手机 App 经媒体服务接入 |
| 内部 Gateway probe | `/ws/gateway` | canonical Gateway frames | 调试、集成、未来全双工需求 |

删除 `run_client` 这一模糊产品命名：脚本、PyCharm 配置、文档、测试和 prompt-safe client label 统一迁移为 `media_simulator`。本地开发入口直接迁移，不保留 `scripts/run_client.py` 兼容 shim；所有仓库内调用点必须在同次变更中更新。

## 4. 统一生命周期与边界

### 4.1 核心运行生命周期

核心仍由 Gateway 管理：

```text
accepted → queued → admitted → active → completed | failed | cancelled
```

统一事实包括：

- `user_id/session_id/turn_id/run_id/trace_id`；
- started、progress、tool lifecycle、response delta、terminal；
- cancel/interrupt 的目标 run 与终态优先级；
- 最终 `response_text`、`annotations`、`output_refs` 和 prompt-safe error。

HTTP、SSE、标准 Gateway WebSocket 与 Media-Agent adapter 都只能消费和投影这些事实，不能重建另一套状态机。

### 4.2 入口交付生命周期

交付状态与核心 run 分离：

```text
HTTP/SSE：request_open → streaming → terminal_sent | disconnected
Media WS：connected → response_sent → acked | ack_timeout | disconnected
```

- SSE 断开会对仍活动的本次 run 发出结构化 cancel，phase 1 不承诺跨连接 replay。
- `chatProgress` 是 Gateway progress 的 Media 投影。
- `chatResponseAck` 只证明媒体应用确认处理最终 delivery，不改变 completed run。
- Media WebSocket 断开、ACK timeout 和 HTTP client disconnect 必须进入 delivery observability，不能把已完成 Runtime 伪装成失败。

## 5. HTTP `/agent/run` JSON 与 SSE

同一请求 schema 和同一 URL，通过 `Accept` 选择表示形式：

```http
POST /agent/run
Accept: application/json
```

保持现有完整 `AgentRunResponse`。

```http
POST /agent/run
Accept: text/event-stream
```

返回 UTF-8 SSE，禁止中间代理缓存。产品事件由 HTTP adapter 从现有 Gateway frame 机械映射：

| Gateway frame | SSE event | payload |
| --- | --- | --- |
| `run.started` | `run.started` | `run_id/turn_id` |
| `stream.chunk` | `response.delta` | 本次新增 `delta` |
| `event.progress` | `run.progress` | prompt-safe progress |
| `event.tool` | `tool.event` | 不可朗读的结构化 tool lifecycle |
| completed `run.end` | `response.completed` | 完整 `AgentRunResponse` |
| failed `run.end` | `run.failed` | 结构化错误与 correlation |
| cancelled `run.end` | `run.cancelled` | cancel reason/correlation |

SSE 终包必须携带与 JSON 模式相同的 `response_text` 和 `annotations`；客户端不从 delta 拼接推断业务终态。现有 HTTP capture 继续负责获得完整 `AgentRunResponse`，流式 adapter 在权威 `run.end` 到达后读取一次 capture 并发出 terminal SSE。

取消使用独立、身份绑定的操作：

```http
POST /agent/runs/{run_id}/cancel
```

SSE `run.started` 必须先交付 `run_id`，Web UI/CLI 才能安全取消当前 run。错误发生在 started 前时返回普通 HTTP 错误；started 后发生的错误必须用 terminal SSE 关闭流。

## 6. Media-Agent adapter 与 capability

`/agent-service/v1` 继续是厂商协议适配器：

| Canonical fact | Media-Agent projection |
| --- | --- |
| progress | `chatProgress`（已协商时） |
| response delta | `chatResponse` `PROCESSING` |
| completed terminal | `chatResponse` `SUCCESS` |
| failed/cancelled terminal | `chatResponse` `FAIL` |
| delivery confirmation | `chatResponseAck` |

新增 `urlCitationAnnotationsV1` client capability。只有客户端在 `assistantControl.clientCapabilities` 中显式声明时，成功终包才在 `message.content.intentResult.annotations` 投影 canonical URL citations；未声明的旧客户端继续只收到 `description`，正文不改写为 Markdown。

`chatProgress`、`chatResponseAck` 和 `urlCitationAnnotationsV1` 只控制 Media wire 的可选字段和 delivery 行为；它们不改变 Tool exposure、Provider 策略或核心 run 状态。

## 7. 两个 CLI

### 7.1 `scripts/agent_cli.py`

- 默认连接 `http://127.0.0.1:8089/agent/run`，发送 `Accept: text/event-stream`。
- 支持交互多轮、显式 `user_id/session_id`、`standard/deep_research`、Ctrl-C cancel。
- 逐 token 打印 `response.delta`；terminal 到达后使用 annotations 显示紧凑来源诊断，不修改正文。
- 提供 `--no-stream` 验证 JSON 模式。
- 不理解 Media envelope、`chatProgress`、ACK、audio/video。

### 7.2 `scripts/media_simulator.py`

- 由当前 `scripts/run_client.py` 迁移，功能保持 Media-Agent-compatible。
- handshake 使用 `clientInfo.clientType=media_simulator`，日志前缀使用 `media-simulator-*`。
- PyCharm 的默认启动参数开启 `stream`；handshake 默认声明 `chatProgress`、`chatResponseAck` 和 `urlCitationAnnotationsV1`。
- 终态打印正文，并以紧凑诊断形式显示本轮被引用来源；它验证协议交付，不代表产品 UI 点击验收。
- 保留现有 Workflow tail、图片响应上限、重连与 ambiguous-delivery 防重发行为。

## 8. PyCharm 一键运行配置

共享配置放在仓库已采用的 `.run/*.run.xml`：

- `Assistant Server`：复用现有 server 配置。
- `Agent CLI`：运行 `scripts/agent_cli.py --server http://127.0.0.1:8089 --interactive`。
- `Media Simulator`：运行 `scripts/media_simulator.py --server http://127.0.0.1:8089 --stream --chat-progress --chat-response-ack --citations --interactive`。

删除或迁移现有含糊的 `Assistant Client` 配置。配置固定使用 `hello_agent` interpreter、`$PROJECT_DIR$` 工作目录、继承本机环境，不写入 API key、token 或 `.env` 内容。Server 必须由用户先点击启动；两个交互客户端保持独立 Run console，避免 compound configuration 抢占输入。

## 9. 代码边界

- Gateway core 继续拥有 session/run/cancel/terminal；若现有 `GatewayTurnFacade` 只能等待终态，增加同层 streaming facade/iterator，而不是在 HTTP route 复制 endpoint relay。
- HTTP entry 只做 request normalization、content negotiation、SSE serialization、disconnect cancel。
- Media entry 只做 vendor envelope、media validation、capability negotiation、canonical event/result projection。
- Runtime citation parser 继续是唯一正文角标到 URL annotation 的解析 owner；入口不得重新用正则猜测。
- `AgentRunResponse` 是 JSON 与 SSE terminal 的统一 HTTP 终态 schema；`RealtimeAgentResult` 是 Gateway adapter 的统一终态边界。

## 10. 错误、兼容和可观测性

- JSON `/agent/run` 保持向后兼容；未传 SSE `Accept` 的调用方行为不变。
- SSE 每个 started run 恰有一个 terminal event；断开触发 best-effort cancel，不发送伪终包。
- Media 旧客户端不声明 citation capability 时 wire shape 不变。
- 重命名同步更新 `docs/authority.toml`、Media/Gateway authority、scripts 索引、测试路径引用和观测 label normalization。
- 观测继续区分 `entry=api|cli|media_agent|media_simulator`；CLI 与媒体模拟器不得共用同一 client type。

## 11. 验证策略

- 使用独立 `tests/tdd/unified-gateway-entry-lifecycle/` 做临时 RED/GREEN，不因功能实现机械扩展永久 core。
- HTTP/SSE：验证 delta 顺序、terminal 完整响应、citation、failure/cancel、disconnect cancel、JSON 回归。
- Media：验证 capability 未声明时兼容、声明后 annotations 投影、ACK/progress 不改变 run terminal。
- CLI：通过离线 fake HTTP/SSE server 验证解析、输出和 cancel；Media simulator 复用离线 vendor WebSocket fixture。
- PyCharm XML：运行脚本级配置校验，确认目标文件、参数、解释器和无 secret。
- 完成前运行对应 TDD、现有 Gateway/Agent-Service 定向回归、documentation authority validator、`compileall`；真实 Provider 不进入 pytest。

## 12. 实施拆分

按依赖顺序拆成三个可独立验收的实施计划批次：

1. Gateway streaming facade + `/agent/run` SSE/cancel + `agent_cli.py`。
2. Media citation capability + `run_client.py` 到 `media_simulator.py` 的迁移。
3. PyCharm 配置、authority/docs 收敛与跨入口回归。

每批只修改其 owning layer；前一批产出的 canonical event/result contract 是后一批唯一依赖。
