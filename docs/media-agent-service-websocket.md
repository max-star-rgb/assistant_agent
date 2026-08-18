# Media-Agent WebSocket 接口权威文档

Last updated: 2026-08-17

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Media-Agent WebSocket envelope、消息字段与当前兼容面的协议权威 |
| Owns | `/agent-service/v1` 的 assistantControl、chat、audio/video ACK、interrupt、chatResponseAck 与 3D callback wire |
| Does not own | Agent Server thread/run/checkpoint、Assistant 推理、Tool/Memory 策略和媒体服务内部实现 |
| 源码与 schema 入口 | `src/assistant_agent/agent_server/media_*.py`、`src/assistant_agent/api/rendering_3d_callback.py` |
| 验证入口 | `docs/authority.toml` 中 `media-agent-protocol.verification` |
| 相邻 authority | Agent Server 部署见 [`agent-server-architecture.md`](agent-server-architecture.md)；视觉能力见 [`multimodal-embedding-architecture.md`](multimodal-embedding-architecture.md) |

## 1. 连接与 envelope

- WebSocket：`ws://<agent_host>:<port>/agent-service/v1`
- 默认本地端口：`8000`；PyCharm 管理的本地 Studio 与媒体联调固定使用
  `scripts/run_server.py --port 8089`。Codex 默认连接该实例，不另起并行 Server。
- 外层是 JSON object，`body` 必须是序列化后的 JSON string。

```json
{
  "message": "chat",
  "sessionId": "vendor-session",
  "body": "{\"chatIndex\":\"chat-1\",...}"
}
```

`sessionId` 只是 vendor 关联值。每次 WebSocket 有独立 connection ID；握手创建 Agent Server
`thread_id`；每个 chat 创建原生 run。connection/session/thread/run/delivery 不得互相替代。

## 2. 握手

首个业务消息应为 `assistantControl`。需要 durable 主动投递的媒体客户端必须声明
`clientCapabilities.chatResponseAck=true`：

```json
{"number":"user-1","callType":"AUDIO","clientCapabilities":{"chatResponseAck":true}}
```

`number` 必填；`callType` 只允许 `AUDIO|VIDEO`。成功响应仍为 `assistantControl`：

```json
{"code":0,"message":"success","phoneNumber":"user-1"}
```

兼容 `assistantControlStart`，其 user 位于 `userInfo.number`，成功响应为
`assistantControlStartAck` 和 `{"code":"OK"}`。同一连接不允许重复绑定握手。

## 3. 文本 chat

请求 body：

```json
{
  "chatIndex": "chat-1",
  "userNumber": "user-1",
  "contents": [
    {"speakerNumber":"user-1","time":"1","speechContent":"你好"}
  ],
  "stream": true
}
```

- `chatIndex`、`userNumber` 和非空 `contents` 必填；`userNumber` 必须与握手一致。
- 每项必须有 `speakerNumber`、`time`；当前 Graph 输入取最后一条非空 `speechContent`。
- `chatIndex` 关联 run，`deliveryId` 只关联本次媒体投递 ACK。
- 媒体入口以 `multitask_strategy=enqueue` 创建 native run。

收到请求后先发：

```json
{
  "message": "chatProgress",
  "body": "{\"chatIndex\":\"chat-1\",\"deliveryId\":\"delivery-...\",\"status\":\"PROCESSING\"}"
}
```

Graph 完成后发唯一成功终包：

```json
{
  "message": "chatResponse",
  "body": "{\"number\":\"user-1\",\"message\":{\"type\":\"BRIEF\",\"chatIndex\":\"chat-1\",\"content\":{\"intentResult\":{\"description\":\"回答\",\"status\":\"SUCCESS\"}}},\"displayOnly\":false,\"display_only\":false,\"sequence\":1,\"final\":true,\"deliveryId\":\"delivery-...\"}"
}
```

`assistantMode` 省略时为 `fast`，也可显式选择 `planning`；旧 `standard|deep_research` 不再接受。媒体适配器
把请求机械转换为标准 HumanMessage content blocks 和根输入 `execution_mode`。最终正文来自 terminal values
中的最新标准 `AIMessage`，适配器不得从 delta 拼接或自行生成业务回答。Memory debounce 是所有入口共享的
主图规则：生成回答后通过官方 Agent Server SDK rollback 同 thread 的旧 pending Memory run，并立即 enqueue
一个新的 30 分钟 delayed Memory run；pending chat run 不受影响。该 orchestration 不扩展媒体 wire，
WebSocket 挂断也不承担 Memory 语义。

## 4. interrupt 与 delivery ACK

`interrupt` 会对该连接已关联的 run 调用 Agent Server 原生 cancel，然后返回：

```json
{"code":0,"message":"interrupted"}
```

`chatResponseAck` body 必须同时携带匹配的 `chatIndex` 和 `deliveryId`；成功返回
`{"code":0,"message":"acknowledged","deliveryId":"..."}`。ACK 不改变 Graph/run 终态，也不触发长期
记忆提交。

### 主动 `chatResponse`

主动消息由显式产品 publisher 写入 durable Store，不经过当前 `AssistantRootGraph`。媒体连接按 native
thread 主动 pull，无需先收到对应 `chat` 请求，仍复用现有
`chatResponse` envelope：

```json
{
  "message": "chatResponse",
  "body": "{\"number\":\"user-1\",\"message\":{\"type\":\"BRIEF\",\"chatIndex\":\"proactive:message-1\",\"content\":{\"intentResult\":{\"description\":\"提醒正文\",\"status\":\"SUCCESS\"}}},\"displayOnly\":false,\"display_only\":false,\"sequence\":1,\"final\":true,\"deliveryId\":\"message-1\"}"
}
```

`message_id` 同时是稳定 `deliveryId`，`chatIndex` 固定为 `proactive:<message_id>`。同一 thread 一次只有
一条 in-flight；客户端必须按 `deliveryId` 幂等展示。durable 消息只有匹配这两个字段的 ACK 才完成，语义为
at-least-once；缺少 ACK capability 时保持 queued，不降级为 socket 写成功。`connection_ephemeral` 只在
enqueue 时已有有效在线 presence 才排队，在线写成功后记为 sent-unacknowledged，离线不补投。

## 5. audio、video 与 3D callback

`audio` 继续做字段校验后的传输层 ACK，不把原始音频写入 Graph State。`video` 校验独立 Annex-B H.264
frame，在 media edge 的工作线程中解码为有界 JPEG window，Graph 输入只携带稳定 `video_id`。Graph worker
的受治理 `media_inspect/live_view_inspect` Tool 通过共享 SQLite frame index 解析该引用；H.264 hex、JPEG
正文和本地路径均不进入 Graph State、prompt 或 Agent Server Store。

解码帧由连接级句柄提交给 Agent Server 内部 `VisualPerceptionModule`；模块内的 `RealtimeVideoObserver`
负责选帧、后台 VLM 调用和视觉语义发布。chat 到达时，媒体入口冻结当时最后一帧并触发 promotion，把
`target_sequence` 绑定到受信入口生成的标准 video content block。`live_view_inspect` 只消费模块已发布的文本：
没有 target 时立即读取 latest；有 target 时有界等待 exact sequence。strict 等待超时或失败时只返回
`pending|failed|stale` 与 `usable_visual_text=false`，不得把旧帧文本当作最后一帧结果；VLM 变慢只延迟该次
严格查询的 run。

保留 callback：

```text
POST /calling-agent-service/v1/{job_or_session_id}/{chat_index}/3d-gen-back
```

它记录中性 3D job artifact；不执行 Assistant Graph。在线连接以 native `thread_id` 订阅完成事件，并机械
投影为带 `TD_MODEL/VIDEO/IMAGE` detail 的 `chatResponse`。当前 hub 是进程内在线交付，不是 Agent Server
run/checkpoint，也不宣称跨进程、离线或 exactly-once 投递；没有 job 且没有在线 subscriber 时 callback
返回失败，让上游决定是否重试。

## 6. 错误与认证

- 非 `v1` 连接返回失败并以 1008 关闭。
- 非法 JSON body、缺字段、identity mismatch、重复关联或未知消息返回结构化失败 envelope。
- custom route 启用 Agent Server auth hook。内部 thread/run 调用走同源公开 API 并转发 identity header；
  Graph、Memory 与 Tool 只读取 Agent Server 注入的 `user.identity`，不得使用 `/noauth` 或信任客户端 Context 身份。
- mock 与 real mode 都使用 tokenless developer principal；`X-Assistant-User` 存在时直接作为 identity，省略时
  使用 `local-developer`。资源 owner 继续按该 identity 隔离，但 identity 未经密钥验证，因此服务端口不得暴露给
  不受信网络。

## 7. 重连与当前限制

相同 `user + vendor sessionId` 通过确定性 UUID 映射到同一个 native thread；重连不会创建第二份对话轴。
custom route 创建 run 时使用 `stream_resumable=true` 与 `on_disconnect=continue`，内部订阅临时断开后从最后
event ID 调用 `threads.join_stream`，而不是重建项目自有 session/runtime。同一连接的重复 `chatIndex` 在创建
第二个 run 前拒绝，后续不同 chat 使用 Agent Server `enqueue`。

媒体 WebSocket 断开时仍 best-effort cancel 该连接的活动 reactive run。主动 durable 行释放 connection lease，
相同 `user + vendor sessionId` 重连到同一 native thread 后重发未 ACK 行；reactive chat 的既有终包仍只由
当前连接关联，不纳入主动 Outbox。Agent Server stream resume 解决执行事件订阅恢复，与媒体 delivery
outbox 保持两套不同语义。当前主动 Store 是单实例/共享持久卷 SQLite；多主机共享事务实现、周期 progress、
durable task 生产者尚未接入该主动 Outbox；后台视觉 observer 只发布视觉语义记录，不伪装 proactive
producer。citation、生成图片 detail、H.264 显式
视觉引用和在线 3D artifact 投影已支持。

视觉资源按进程与连接分层：Agent Server custom FastAPI lifespan 拥有进程级
`VisualPerceptionModule`，仅在进程 shutdown 时关闭；每个媒体 WebSocket 只拥有并关闭自己的
`VisualPerceptionSession`、observer 与 lease，不得关闭或替换进程模块。临时 graph factory 与
schema/history/state 请求同样不拥有该进程资源。

## 8. 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/contract/test_gateway_contract.py \
  tests/tdd/agent_server_native_runtime/test_media_custom_route.py

MULTIMODAL_AGENT_PROVIDER_MODE=mock LANGSMITH_TRACING=false \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  evals/system/incubating/agent_server_native_runtime/checks_deployment.py
```
