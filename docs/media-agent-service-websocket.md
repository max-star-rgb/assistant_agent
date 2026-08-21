# Media-Agent WebSocket 接口权威文档

Last updated: 2026-08-21

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Media-Agent WebSocket envelope、消息字段与当前兼容面的协议权威 |
| Owns | `/agent-service/v1` 的 assistantControl、chat、audio/video ACK、interrupt、chatResponseAck 与 3D callback wire |
| Does not own | Agent Server thread/run/checkpoint、Assistant 推理、Tool/Memory 策略和媒体服务内部实现 |
| 源码与 schema 入口 | `src/assistant_agent/agent_server/media_*.py`、`src/assistant_agent/api/rendering_3d_callback.py` |
| 验证入口 | `docs/authority.toml` 中 `media-agent-protocol.verification` |
| 相邻 authority | Agent Server 部署见 [`agent-server-architecture.md`](agent-server-architecture.md)；视觉能力见 [`visual-perception-architecture.md`](visual-perception-architecture.md) |

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
`assistantControlStartAck` 和 `{"code":"OK"}`。legacy AR 链路中的后续 `chat.userNumber` 和
`video.userNumber` 沿用旧协议的独立号码口径，不强制与 `userInfo.number` 相等；native thread 和认证身份
仍以握手身份为准。现代 `assistantControl` 的后续媒体号码必须与 `number` 一致。同一连接不允许重复绑定握手。

为兼容 2026-08-13 前的 AR 媒体链路，连接首个业务消息直接为 `chat` 时，媒体入口使用该帧的
`sessionId/userNumber` lazy 创建 native thread，再按正常 chat 流程发送 `chatProgress` 和创建 run；该兼容
路径不具备 control 协商的客户端能力和 durable 主动投递能力。非开发者认证连接仍要求 `userNumber` 与
Agent Server `user.identity` 一致。

“完成 VIDEO 握手”的结构化定义是：服务端已成功校验并绑定首个 control 消息、创建该连接对应的 native
`thread_id`，且绑定的 `callType` 等于 `VIDEO`。它不要求已经收到第一帧。后续 chat run 由媒体入口把该事实
投影为 `AssistantRunContext.realtime_media_mode="video"`；AUDIO 握手和未完成 control 的连接均为 `none`。
每次 chat 开始时，媒体入口还会在进程视觉模块中冻结当时的 video IDs 与严格窗口，并只把服务端签发的 opaque
capability token 放入 run context；token 按认证身份与 thread 校验，run 结束即撤销。窗口不进入用户 message，
后续并发 chat 更新 session 投影也不能改变已创建 run 的视觉检索上界。撤销同时绑定 task done callback，覆盖
协程首次执行前即被取消的路径。

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

- `chatIndex`、`userNumber` 和非空 `contents` 必填；现代 `assistantControl` 链路中 `userNumber`
  必须与握手一致，legacy `assistantControlStart` 保持独立号码口径兼容。
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

`stream=true` 时，Graph 原生 `messages/partial` 中新增的 assistant 文本会沿用媒体既有的
`chatResponse` 增量结构发送：

```json
{
  "message": "chatResponse",
  "body": "{\"message\":{\"chatIndex\":\"chat-1\",\"content\":{\"intentResult\":{\"description\":\"我先查一下。\",\"status\":\"PROCESSING\"}}},\"displayOnly\":false,\"display_only\":false,\"sequence\":1,\"final\":false}"
}
```

媒体入口创建原生 run 时开启 `stream_subgraphs=true`，因为 fast Agent 是父图中的原生子图；否则 Provider
虽已产生 token，顶层 Agent Server stream 仍看不到子图消息。模型在 ToolCall 前生成的普通文本按上述结构
原样增量发送；若模型直接产生 ToolCall 而没有任何前导正文，媒体适配器不合成提示语，等待模型后续正文或
终态。ToolCall name/arguments、ToolMessage、原生 updates 与 custom Tool 生命周期均不进入媒体正文。

Graph 完成后发送成功终包：

```json
{
  "message": "chatResponse",
  "body": "{\"number\":\"user-1\",\"message\":{\"type\":\"BRIEF\",\"chatIndex\":\"chat-1\",\"content\":{\"intentResult\":{\"description\":\"回答\",\"status\":\"SUCCESS\"}}},\"displayOnly\":true,\"display_only\":true,\"sequence\":2,\"final\":true,\"deliveryId\":\"delivery-...\"}"
}
```

`assistantMode` 省略时为 `fast`，也可显式选择 `planning`；旧 `standard|deep_research` 不再接受。媒体适配器
把请求机械转换为标准 HumanMessage content blocks 和根输入 `execution_mode`。`stream=true` 只投影
`AIMessageChunk` 的 string content 或 `text|output_text` block；当 `messages/metadata` 存在时，明确标记为
非 `model` 节点的 chunk 会被排除；metadata 缺失时按标准 assistant chunk 降级投影，避免原生流存在但媒体侧
只能收到终包。planner 等已标记的其他节点内部文本、tool-call name/arguments、ToolMessage 和 updates 不进入媒体正文。
Agent Server 的 `messages/partial` 是同一 message 的累计快照，适配器按 message ID
计算 append-only delta；模型生成的 Tool 前导文本与工具后的下一条 assistant message 之间若均无换行，适配器在新消息首包
补一个换行。中间包按 `sequence` 递增且 `final=false`，不携带 `deliveryId`；`stream=false` 不发送中间包。

最终正文仍来自 terminal values 中的最新标准 `AIMessage`，适配器不把 delta 拼接成业务终态。成功终包发送该
最新消息中尚未流出的后缀，使用最后一个 `sequence`、`final=true`，并独占 `deliveryId`；若最终文本与已流出的
最新 assistant message 不一致，则完整发送权威终态，避免丢失正文。已发送中间包且终包不含媒体 detail 时，
终包的 `displayOnly/display_only=true`；citation 的 `fullDescription` 始终保留完整权威终态。

若当前用户轮次存在成功的 `shopping_search` 标准 `ToolMessage`，媒体终态会从其结构化 `artifact` 确定性追加
兼容购物卡片块，而不是要求模型生成协议标签：

```text
<detail>
1. 京东 - 商品名 2599元 <link>https://...</link><pic>https://...</pic>
</detail>
```

只使用当前轮最新成功购物结果，最多三项；商品名会移除协议标签和控制字符，购买链接与图片必须是无空白、
无尖括号的 HTTP(S) URL。没有安全完整的链接与图片时不输出对应卡片，也不会复用历史轮次结果。

Memory debounce 是所有入口共享的
主图规则：生成回答后通过官方 Agent Server SDK rollback 同 thread 的旧 pending Memory run，并立即 enqueue
一个新的 30 分钟 delayed Memory run；pending chat run 不受影响。该 orchestration 不扩展媒体 wire，
WebSocket 挂断也不承担 Memory 语义。

当客户端声明 `clientCapabilities.urlCitationAnnotationsV1=true`，媒体入口只读取最新终态 `AIMessage` 自身的
`provider_search_sources`，把正文中实际存在的 `[n]` 角标投影为 `intentResult.annotations`，并保持
`fullDescription` 与原正文一致。历史回复和中间 tool-call AIMessage 的来源不得聚合；无角标、索引不匹配或
非 HTTP(S) URL 的来源不进入 wire。

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

`audio` 继续做字段校验后的传输层 ACK，不把原始音频写入 Graph State。`video` 先在 WebSocket 热路径完成
字段校验，再按连接内 wire 顺序进入后台解码队列；后续 chat 不等待尚未完成的视频解码。后台任务在 media edge
的工作线程中把独立 Annex-B H.264 frame 解码为有界 JPEG window；连接级待处理消息数有固定上限，满时返回
结构化失败而不无限保留媒体正文。Graph 输入只携带稳定 `video_id`。Graph worker
的受治理 `live_view_inspect` Tool 通过共享 SQLite frame index 解析该引用；H.264 hex、JPEG
正文和本地路径均不进入 Graph State、prompt 或 Agent Server Store。

媒体入口给摄像头引用固定标记 `source=live_camera`；用户主动上传的图片或视频必须由普通请求入口标记为
`source=uploaded`，交给独立的 `uploaded_media_inspect`，两类引用不能互相替代。VIDEO control 成功后即允许
模型看到 `live_view_inspect`；当当前 `user/thread/as-of sequence` 已产生可检索视觉文本时，才进一步暴露
`visual_memory_search`。`visual_reminder_manage` 则在首个视频包成功解码、`video_id` 已绑定后才暴露；此时
媒体连接把连接级 reminder manager 注册到视觉 Runtime，并将提醒命中机械投影为当前 WebSocket 上的主动
`chatResponse`。握手后尚未收到有效帧、解码失败或连接已关闭时均不可用。这些条件暴露不依赖 Skill 加载。

媒体 wire 只负责把每个成功解码帧提交给连接级视觉句柄。chat 到达时只冻结当时已经成功解码的帧；已入队但
尚未完成解码的帧不进入该轮窗口。随后它把视觉模块冻结得到的可信
`window_id`、`window_start_sequence` 和 `target_sequence` 绑定到标准 `source=live_camera` video content
block，不传 JPEG、Provider client 或 task。逐帧并发、semantic keyframe、目标帧等待、ready/missing 结果和
晚到帧处理均以 [`visual-perception-architecture.md`](visual-perception-architecture.md) 为唯一权威。

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

生成图片使用标准 `ToolMessage(content, artifact)` 双通道，公共 Graph 不改写最终 `AIMessage`。媒体入口从
当前用户轮次成功的 `image_generation` ToolMessage 中优先读取 `artifact.images[].output_ref`；对旧 checkpoint
兼容读取 `download_urls` / `output_ref`。随后读取有界本地图片，并继续按本协议投影为
`intentResult.detail[].type=IMAGE` 的 Base64 正文。Studio 当前只显示最终文本，不承诺渲染 Tool artifact。

媒体连接只关闭自己取得的视觉 session handle，不关闭 Agent Server 进程级视觉 owner；完整资源所有权和
清理规则见视觉 authority 与 Agent Server authority。

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
