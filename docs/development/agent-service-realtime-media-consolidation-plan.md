# Media-Agent WebSocket 单入口迁移实施计划

状态：实施中

创建时间：2026-07-21

目标入口：`/agent-service/v1`

## 1. 背景与决策

当前仓库同时存在两套媒体 WebSocket 入口：

- `/agent-service/v1`：真实 Media Service 与 Agent 的厂商协议入口，wire contract 以
  `docs/media-agent-service-websocket.md` 为权威，接收 `assistantControl`、`chat`、
  `audio`、原始 H.264 `video` 和 `interrupt`。
- `/ws/realtime/media`：在真实厂商协议接入前建立的 Media Relay 预留入口，接收
  `session.start`、`transcript.final`、`run.cancel`、`config.update`、`session.end`
  等规范化事件，只携带媒体引用，不接收原始 H.264。

Git 历史表明，`/ws/realtime/media` 于 2026-07-02 随 Gateway 入口层首次加入；
`/agent-service/v1` 于 2026-07-06 根据真实 Media Service 协议加入。两条路线后续并行扩展，
形成重复的媒体接入面和不一致的中断语义。

本次迁移作出以下决策：

1. `docs/media-agent-service-websocket.md` 是媒体与 Agent 的唯一 wire protocol 权威。
2. `/agent-service/v1` 是唯一保留的 Media Service WebSocket 入口。
3. Gateway 继续作为 session、run、cancel、interrupt 和 runtime 生命周期边界；迁移不新增
   第二套 Agent loop。
4. 保持厂商 envelope 和字段兼容，不要求 Media Service 改发 `transcript.final` 或媒体引用。
5. 只迁移真实协议需要的能力，不把旧 Media Relay 的假设性协议字段机械塞入厂商协议。

## 2. 当前差异

| 能力 | `/agent-service/v1` 当前状态 | `/ws/realtime/media` 当前状态 | 迁移决定 |
| --- | --- | --- | --- |
| 原始 H.264 | 支持并写入受限视频上下文 | 不支持，只接收 `video_ids` | 保留 Agent-Service 实现 |
| 文本 turn | `chat` 经本地 `GatewayTurnFacade` | `transcript.final` 经共享 `GatewayBridge` | 保留 `chat` wire contract |
| 显式中断 | `interrupt` 只返回 ACK | 可映射为 `run.cancel`/interrupt | 在 Agent-Service 实现真实取消 |
| 断线取消 | 已取消 chat task，并由 facade 发送 `run.cancel` | Bridge 负责断线取消 | 保留并补测试 |
| realtime task state | 已声明 `supports_realtime_task_state` | 已声明同一 capability | 收敛对旧 source 的硬编码 |
| 隐式语义打断 | 无厂商字段 | `transcript.final` 可触发仲裁 | 不迁移；不属于权威协议 |
| config update | `assistantControl` 承载厂商控制信息 | 独立 `config.update` | 保留 `assistantControl` |
| session end | WebSocket 断开清理连接资源 | 显式 `session.end` | 以连接关闭为厂商生命周期 |

## 3. 目标架构

```text
Media Service
    |
    | assistantControl / chat / audio / video(H.264) / interrupt
    v
/agent-service/v1
    |
    | 厂商协议校验、响应 envelope、H.264 entry ingestion
    v
GatewayTurnFacade / GatewaySessionManager
    |
    | run、cancel、interrupt、history、realtime task state
    v
GatewayAgentAdapter
    v
AssistantRuntimeApp -> AgentGraphRuntime
```

迁移完成后不再暴露 `/ws/realtime/media`，但保留产品无关的 `/ws/gateway`，供明确使用
normalized Gateway frames 的非媒体客户端使用。

## 4. 不变量

- 不改变 `/agent-service/v1` 的 URL、外层 `message`、可选 `sessionId` 和字符串化 `body`。
- 不改变 H.264 Annex-B Hex、视频 ACK、`chatResponse` 流式包和应用层 ACK 语义。
- `interrupt` 成功 ACK 表示 Agent 已接受中断请求；有活动或排队 turn 时必须向 Gateway 发起取消。
- 被中断 run 的后续 token、终包和工具结果不得作为可播放/可展示响应发送给 Media。
- 同一连接继续可接收新 `chat`；中断不关闭 WebSocket，不清空已完成的会话 history。
- 连接断开继续清理 chat task、Gateway、observer、Provider session、临时帧和视频上下文。
- 默认验证只使用 mock/local/offline，不调用真实 Provider。

## 5. 分阶段实施

### 阶段 A：固化 Agent-Service 中断契约

1. 为连接状态增加活动 delivery/turn 的可取消跟踪，使用 Gateway 分配的
   `turn_id`、`run_id`，不从用户 payload 推断运行身份。
2. 将 `InterruptHandler` 从纯 ACK 改为调用连接级取消服务：
   - 取消当前活动 turn；
   - 取消尚未开始的同连接排队 turn；
   - 通过 Gateway `run.cancel` 使用结构化 `source=media_interrupt` 和安全 reason；
   - 无活动 turn 时保持幂等成功 ACK。
3. 中断期间禁止被取消 delivery 继续发送 `chatResponse` delta 或终包。
4. 正确结算 delivery registry、turn timing、trace/lifecycle 状态，避免长期停留在 pending。
5. 保持 WebSocket 主接收循环非阻塞，使 `interrupt` 能在长 turn 运行时立即处理。

验收：活动 run 收到一次 Gateway cancel；Media 收到成功 `interrupt` ACK；旧 run 不再输出；
连接可继续完成下一轮 `chat`。

### 阶段 B：收敛 realtime 生命周期标识

1. 审计 `realtime_media_websocket` 字符串判断，区分：
   - 旧路由 transport/source 专用逻辑；
   - 可由 `EntryAdapterCapabilities` 表达的通用 realtime 能力；
   - 厂商显式中断不需要的隐式语义仲裁逻辑。
2. realtime task state、pending tool、TTS/display 和 artifact reuse 继续由
   `AGENT_SERVICE_ENTRY_CAPABILITIES` 驱动。
3. 为 Agent-Service 请求补齐必要、可信且 prompt-safe 的 entry source/request kind；
   不伪造 `/ws/realtime/media` 身份。
4. 删除只为旧 Media Relay `transcript.final` 服务的 source 分支；保留普通
   `/ws/gateway` 所需的可信身份逻辑。

验收：Agent-Service 的 realtime task state 行为不回退；普通 Gateway WebSocket 不受影响；
代码中不再依赖旧媒体路由才能启用厂商入口能力。

### 阶段 C：删除旧 Media Relay 入口

1. 从 `gateway_websocket.py` 删除 `/ws/realtime/media` route、event mapper、媒体事件常量和
   仅由该 route 使用的校验代码。
2. 保留 `/ws/gateway` normalized frame route 和共享 Gateway runtime。
3. 删除只服务旧入口的脚本：
   - `scripts/realtime_media_client.py`
   - `scripts/run_realtime_call_simulator.py`
4. 更新 `scripts/run_server.py`、`scripts/README.md` 和其他导航，不再宣传旧 URL。
5. 删除 `REALTIME_MEDIA_ENTRY_CAPABILITIES` 及失去调用方的分支、测试 fixture 和导出。

验收：应用路由表不再包含 `/ws/realtime/media`；`/agent-service/v1` 和 `/ws/gateway`
仍可初始化；仓库非历史材料中没有旧入口引用。

### 阶段 D：权威文档收敛

1. 更新 `docs/gateway-architecture.md`：实时媒体产品路径只描述 `/agent-service/v1`，并明确
   厂商入口内部通过 Gateway 管理生命周期。
2. 更新 `docs/media-agent-service-websocket.md`：把 `interrupt` 从兼容 ACK 改为真实取消语义，
   记录幂等、响应和旧 run 输出抑制规则。
3. 更新 README/脚本说明；历史 `docs/development/**` 保留为历史记录，不作为当前权威。
4. 使用 `rg` 确认当前源码、测试、脚本和 root authority 不再把旧入口写成正式能力。

## 6. 测试决策

主要决策：**ADD**。

理由：`interrupt` 从纯 ACK 变为真正取消运行，属于稳定外部协议变更，同时涉及并发、取消、
事件顺序和 stale output 抑制；现有最小安全网不能证明该行为。

新增一个聚焦的 Agent-Service WebSocket 中断契约测试文件，优先验证外部可观察行为：

1. 活动 `chat` 期间发送 `interrupt`，收到成功 ACK；
2. 当前 Gateway run 被取消，旧 `chatResponse` 不再发送；
3. 同一连接随后发送新 `chat` 可以成功；
4. 没有活动 run 时 `interrupt` 幂等成功；
5. WebSocket 断开仍取消活动 run 并完成清理。

不在多个层级重复断言同一取消行为；Gateway 内部已有取消机制由现有安全网复用。

阶段验证命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q <新增中断测试文件>
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
git diff --check
rg -n '/ws/realtime/media|realtime_media_websocket' \
  src tests scripts docs/*.md README.md
```

## 7. 提交策略

按可回滚阶段提交，不把全部迁移压成一个提交：

1. `docs: plan media websocket entry consolidation`
2. `fix: make agent service interrupt cancel gateway turns`
3. `refactor: consolidate agent service realtime lifecycle`
4. `refactor: remove legacy realtime media websocket`
5. `docs: make agent service the canonical media websocket`

每个行为提交只包含本阶段相关文件并通过相应定向测试。最终提交前运行完整离线安全网。

## 8. 风险与回退

- 最大风险是中断 ACK 已发送但旧 run 仍产生输出。实现必须先建立取消/输出抑制状态，再发送 ACK。
- 直接取消 asyncio chat task 只能作为触发手段；必须保证 facade 向 Gateway 发出 `run.cancel`，并正确
  结束 delivery/trace 状态。
- 删除旧入口前必须确认仓库内调用方已全部迁移。仓库外调用方无法由源码证明，部署切换时仍需检查
  网关配置和 Media Service 实际连接 URL。
- 每阶段均可按独立提交回退；在阶段 C 之前，旧入口仍可临时恢复用于对照，但不再新增功能。

## 9. 完成条件

- Media Service 只通过 `/agent-service/v1` 与 Agent 通信。
- H.264、chat、stream、ACK、interrupt、disconnect 均在同一入口闭环。
- `interrupt` 真正取消 Gateway run，旧输出不会送达 Media。
- `/ws/realtime/media` 路由及非历史代码、脚本和权威文档引用全部删除。
- `/ws/gateway` 和 `AgentGraphRuntime` 架构边界保持不变。
- 定向中断测试和默认离线 pytest 全部通过。
