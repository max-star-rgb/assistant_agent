# Agent Server 原生主动投递设计

## 目标

在不恢复自研 Memory/Delivery Runtime、不把 WebSocket 对象放入 Graph State 的前提下，让静态
Assistant Graph 的任意前序业务节点能够产生主动消息意图，并由固定 `delivery_dispatch` 节点可靠写入
Outbox；Agent Server 的 `/agent-service/v1` custom route 通过现有 `chatResponse` / `chatResponseAck`
协议完成在线发送、ACK 和同一会话重连后的补投。

本设计以 `cqy@c52af2ca` 的 native-edge Graph 为基线。LangGraph 原生 edge、conditional edge、
checkpoint snapshot 和 Runtime 继续作为执行事实源，不恢复 `continuation -> anchor -> gate` 手写状态机。

## 已确认的产品语义

- durable 主动消息绑定 native `thread_id`。该 ID 已由 `user + vendor sessionId` 确定性映射；重连同一
  会话继续投递，不自动投递到同一用户的其他设备或其他会话。
- `message_id` 同时作为稳定 `deliveryId`；主动消息使用
  `chatIndex = proactive:<message_id>`。媒体端按 `deliveryId` 去重。
- 同一 thread 串行投递，一次最多一条 in-flight。WebSocket 写成功不等于 durable delivery 成功；只有
  匹配的 `chatResponseAck` 才进入 acknowledged 终态，整体语义为 at-least-once，不宣称 exactly-once。
- `durable` 在离线或断线后保留并于同一 thread 重连后继续；`connection_ephemeral` 仅在入队时已有该
  thread 的在线连接时发送，否则进入 `skipped_offline`，以后不补投。
- durable 消息要求客户端握手声明 `clientCapabilities.chatResponseAck=true`。缺少该能力时不发送并保留
  queued，同时记录 `ack_capability_missing`。ephemeral 消息允许以 WebSocket 写成功作为
  `sent_unacknowledged` 终态。
- 本阶段完整支持单实例或共享持久卷部署。存储通过薄协议隔离；多副本部署后续替换为 PostgreSQL 等共享
  事务实现，不把 LangGraph `BaseStore` 强行当消息队列。

## Graph 架构

Graph 拓扑在 compile 前固定：

```text
START
  -> prepare_invocation
  -> memory_recall
  -> assistant / tool loop
  -> compose_response
  -> publish_response
  -> conditional pending delivery
       empty   -> memory_commit
       pending -> delivery_dispatch -> memory_commit
  -> END
```

任意位于 `publish_response` 之前的业务节点都可以通过纯函数 helper 向严格 State 的
`pending_deliveries` 追加数据。投递不会立刻打断当前语义节点，而是在主回答完成 publish 后统一处理。
因此 dispatch 只有一个固定返回点，不需要 checkpoint 一份“返回到哪个 node”的控制字段。

`delivery_dispatch` 是 Graph 内唯一 Outbox 写 authority：

- `invoke` / `resume` 将 intent 绑定可信 `user_id/thread_id/run_id/trace_id` 后幂等 enqueue；
- `replay` / `fork` 清除继承的 pending、记录 skipped，并禁止产生外部写入；
- 全部 enqueue 成功才清空 pending；中途失败让 LangGraph 保留上一个 checkpoint，重试依靠稳定
  `message_id` 去重；对于 `connection_ephemeral`，enqueue 在同一事务中读取当前 thread 的有效 presence
  lease，无在线 lease 时直接写入 `skipped_offline`；
- 节点只等待本地持久入队，不等待 WebSocket、ACK 或媒体重连。

`pending_deliveries` 与 dispatch outcome 是 prompt-invisible observability data。主 LLM、Tool observation、
Memory context 和媒体输入均不得消费这些字段。

## Outbox 与状态机

定义薄的 `ProactiveDeliveryStore`，Graph 只依赖其 `enqueue()`；custom route pump 使用 claim、ack、release
等传输方法。SQLite 实现保存规范化 envelope 以及以下状态：

```text
queued
  -> leased
       -> acknowledged            durable + ACK
       -> sent_unacknowledged      ephemeral + socket write
       -> queued                   disconnect / lease expiry / ACK timeout
  -> skipped_offline              ephemeral + enqueue 时无在线连接
```

稳定 `message_id` 已存在且完整 envelope 相同视为幂等；相同 ID 但 identity、target、kind、content 或
delivery mode 不同必须 fail closed。claim 使用 SQLite 事务和短租约，避免同一进程内重复 pump；每个 thread
只取最早一条可投递消息。durable ACK 超时后释放回 queued，并采用有界退避；本阶段不引入独立 retry
scheduler、worker queue 或 dead-letter Runtime。

同一 SQLite store 保存 thread presence lease，但不保存 WebSocket 对象。custom route 在握手、心跳和断开时
按 `thread_id + connection_id` 更新 lease；Graph enqueue 只读取其是否仍有效。默认 ACK timeout 15 秒、
claim lease 30 秒，轮询和退避均可由配置覆盖。

Outbox 记录最小 outcome/attempt metadata，用于 ACK、重连与诊断，不进入 checkpoint。acknowledged 与
skipped 记录保留用于幂等和审计；清理属于后续运维策略，不影响当前投递语义。

## Media custom route

每个成功完成 `assistantControl` 握手的连接启动一个 thread-specific pull pump。pump 只处理与本连接
`thread_id`、认证 user 匹配的消息：

1. 依据在线能力 claim 最早消息；
2. 用现有 `chatResponse` envelope 机械投影正文，不调用 LLM、不启动新的 Graph run；
3. durable 消息发送后等待同连接的 `chatResponseAck`；
4. ACK handler 同时校验 `thread_id/chatIndex/deliveryId`，原子标记 acknowledged 并唤醒 pump；
5. WebSocket 断开时取消 pump、释放未 ACK lease；同一 `user + vendor sessionId` 重连后新 pump 继续；
6. reactive chat 的既有 delivery ID 与 proactive message ID 共用 ACK frame，但存储和关联保持明确。

custom route 只拥有连接、wire projection 与 ACK。它不读取 Graph checkpoint、不决定何时产生业务通知、
不执行 Tool/Memory，也不维护另一套 session/run/checkpointer。

## 失败与安全边界

- Outbox 不可用时 `delivery_dispatch` 失败，使当前 Graph run 不越过 memory commit；不会谎报已投递。
- durable 客户端未声明 ACK 能力时保持 queued 并记录问题，不执行降级发送。
- payload 投影、身份或 ACK 不匹配返回结构化协议错误，不能确认或删除其他 thread 的消息。
- 断线、socket 写失败、ACK timeout 均允许 durable 重投；媒体端必须按 `deliveryId` 幂等展示。
- connection_ephemeral 在 enqueue 时的“在线”事实来自同一 store 的短期 presence lease；WebSocket 仍只由
  custom route 持有。Graph dispatch 只读取最小在线判断，不持有连接对象。
- SQLite 文件只允许本地受控路径；不保存 token、Authorization header、Provider 响应或真实媒体正文。

## 测试与验收

临时 RED/GREEN 测试放入 `tests/tdd/native-proactive-delivery/`，覆盖：

- native Graph 静态注册 dispatch，conditional edge 只在 pending 非空时经过它；
- publish 发生在 enqueue 之前，dispatch 后固定进入 memory commit；
- invoke/resume 幂等入队，replay/fork 不写外部副作用；
- SQLite envelope 冲突、单 thread 串行 claim、lease release、ACK 与断线重投；
- durable 缺 ACK capability 时保持 queued；ephemeral 离线 skip、在线写成功终结；
- 重连相同 thread 重发未 ACK 消息，不向其他 thread/user 泄漏；
- `chatResponseAck` 同时校验 stable delivery ID 与 proactive chatIndex；
- Graph State/checkpoint 与 LLM prompt 中不出现 Store、WebSocket、ACK waiter 或连接对象。

完成时运行 feature TDD、默认 core pytest、媒体 simulator/help、源码编译、文档 authority validator 与
`git diff --check`。默认 provider mode 为 mock，不调用真实 Provider。

## 明确不做

- 不支持多副本共享 Outbox 的生产实现；只保留替换接口。
- 不把 Outbox 包装为 LangGraph `BaseStore`，也不建立新的 Delivery Service/PluginHost。
- 不实现 exactly-once、跨用户/跨设备广播、优先级队列、死信队列或运营管理 UI。
- 不改变媒体服务现有 envelope 名称；媒体侧只需声明 ACK capability，并按稳定 `deliveryId` 去重。
