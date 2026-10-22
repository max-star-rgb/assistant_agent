# Media-Agent WebSocket 接口权威文档

Last updated: 2026-10-22

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Media-Agent WebSocket envelope、消息字段与当前兼容面的协议权威 |
| Owns | `/agent-service/v1` 的 assistantControl、chat、audioStreamV1 双向音频、audio/video ACK、interrupt、chatResponseAck 与 3D callback wire |
| Does not own | Agent Server thread/run/checkpoint、Assistant 推理、Tool/Memory 策略和媒体服务内部实现 |
| 源码与 schema 入口 | `integrations/qwen_audio_agent/src/media-entry.mjs`、`src/assistant_agent/agent_server/media_*.py`、`src/assistant_agent/agent_server/rendering_3d_callback.py`、`src/assistant_agent/media/generated_artifacts.py`、`media/image_to_3d*.py` |
| 验证入口 | `docs/authority.toml` 中 `media-agent-protocol.verification` |
| 相邻 authority | Agent Server 部署见 [`agent-server-architecture.md`](agent-server-architecture.md)；视觉能力见 [`visual-perception-architecture.md`](visual-perception-architecture.md) |

## 1. 连接与 envelope

**当前支持范围：** Studio/SDK 直接运行 `assistant-business-v2`。`/agent-service/v1` 的旧 `chat` 仍明确拒绝；
声明 `clientCapabilities.audioStreamV1=true` 的媒体连接通过本机 Qwen Gateway 的公开 GCP 7 接入实时模型，
双向音频合同见第 5.1 节。未声明的连接仍只做音频 ACK；ACK 不代表模型已理解。
独立视觉流水线和 callback 保留。下文旧 Graph chat 流协议仅作为待迁移合同，不表示已重新开放。

- 对外媒体 WebSocket：`ws://<agent_host>:3101/agent-service/v1`；`8081/agent-service/v1` 保留，二者使用同一适配器。
- 两个 Gateway 端口均支持 `/agent-service/v1?number=00860332`：以 URL 号码自动初始化实时会话，无需先发送 `assistantControl` 或声明 `audioStreamV1`，也不再等待控制握手超时。
  初始化时返回 `audioConnecting`，成功后返回 `audioReady`；期间输入保序等待，不设置接收队列的消息条数或累计字节上限，不再因 `MEDIA_BACKPRESSURE` 断连。初始化失败仍返回 `audioError` 并断连，单消息大小及音频格式校验保留。
  无 URL 号码的旧客户端仍可通过 `assistantControl` / `assistantControlStart` 提供号码；带 URL 号码的连接也可发送旧控制消息，但不能更换身份。
  音频格式校验继续执行；`chat` 仍只确认 ASR 字幕，不触发重复模型输入。首个 `video` 自动建立内部视觉链路。
  本段自动初始化规则仅适用于 Gateway 的 3101/8081；下文旧 8088 媒体协议的强制握手及能力声明规则不适用于这两个端口。
- `integrations/qwen_audio_agent/src/media-entry.mjs` 在 Gateway 同一进程监听 `8081`，只开放此路径。
  音频直接适配到 Gateway 的公开 GCP 会话；3101 与 8081 均监听 `0.0.0.0`，接受局域网连接。
  `8088` 继续承载业务 Agent Server 和内部视觉服务，不再是媒体客户端的目标地址。
- 唯一后台 Agent Server 使用 `8088`；冗余 8089 实例停用。
- 外层是 JSON object，`body` 必须是序列化后的 JSON string。

Gateway 媒体适配器只负责连接身份、消息转换、Realtime 事件转发及连接资源释放，不创建业务 run。
后台 Tool 声明的 `<detail>...</detail>` 附加文本通过原生任务 `artifacts[].parts[].text`
（`mediaType=text/plain`）传到前台。只取当前用户轮次同名 Tool 的最新成功声明；最新失败不回退旧卡片，
不从模型正文提取，不影响原有图片收集。卡片去重，总长最多 16000 字符，超限不截断标签。
当前会话任务完成后，前台独立发送一个 `chatResponse`，将原文放入
`message.content.intentResult.description`（不是图片 `detail[]`），`chatIndex/deliveryId=detail:<taskId>`，
`displayOnly/display_only=true`、`final=true`。同连接同任务仅发送一次；普通回答和音视频包保持既有行为。
`media.detail_delivery` 表示 socket 写出成功，不代表端侧展示确认；尚不支持断线重投。
后台任务完成后的图片也由此适配器交付：接收当前 GCP session 的 `task.completed` / 已完成 `task.updated`，
只消费显式 `artifacts[].parts` 的图片引用，通过身份 header 读取同一 Agent Server 的 `/artifacts/...`，
转换为 `chatResponse.message.content.intentResult.detail[]` 中的 `IMAGE`、`imageId` 与 Base64 `image`。
本地 mock 和真实图片使用同一路径；每个连接内同一任务只交付一次，不能串入网页或其他媒体会话。
最多交付 4 张、每张 25 MiB；禁止外部 URL/重定向，检查媒体类型与图片头。图片读取异步进行，不阻塞音频事件转发，断连取消读取。
同一媒体 WebSocket 由单个发送器管理实时消息与图片两个队列：各队列内部保序，待发音频、控制和 ACK 优先于尚未开始写出的图片。
图片一旦开始写出不可被应用层音频抢占；等待写出期间不再因自身图片超过 2 MiB 缓冲而报协议错误。图片写出时限为 30 秒，其他消息为 5 秒，从实际开始写出计时；超时或真实写入错误关闭失效连接，拒绝剩余队列。
上行音频转发不等待下行 `audioResponse` 写出，图片下行缓慢不会阻断相反方向的音频接收。图片保持原内容和既有 Base64 envelope，不要求媒体新增接口。
`media.image_delivery` 记录任务 ID、已写出图片数和读取失败标记；写出成功不等于端侧已展示。
读取失败只报告图片传输失败，保留语音连接，不重跑生成任务；断线后的图片重投/ACK 持久化尚未实现。
音频上行按 100 ms（16 kHz、PCM16、单声道共 3200 字节）合并或拆分后提交 Gateway；发送暂停 100 ms 后发送不足一包的剩余样本，不补零、不丢弃尾音。
这是每包音频时长，不是额外按墙钟限速；历史积压仍按序转发。既有 `audioResponse` 字段保持兼容，表示适配器已接收数据进入转发流程，不代表模型已消费。
连接关闭日志区分收到的 `audioPackets` 与提交 Gateway 的 `uplinkChunks`；视频保持最多 1 FPS。
已建立的 Gateway 会话收到普通 command/response `error` 时记录脱敏诊断，不立即断开；
`client_occupied`、`session_expired`、协议版本不兼容、会话停用或 GCP WebSocket 关闭仍终止连接。
`voice.connection` 的 disconnected/reconnecting/unavailable 状态允许上游原生恢复最多 15 秒，
期间通知 `audioConnecting` 并中断旧播放，恢复后通知 `audioReady`；超时返回
`audioError.code=GATEWAY_RECOVERY_TIMEOUT` 并释放连接。初始化失败仍立即结束。
Gateway 日志带 UTC 时间，`media.gateway_event` 只保留事件类型、已知状态和白名单错误码，不记录原始错误正文。
后台视觉连接释放只清理本地状态与任务，不再向已关闭的 WebSocket 发送画面清空事件。
会话初始化与对外 `audioReady` 通知分开：URL 自动接入在初始化完成后通知；兼容控制消息保持先 ACK、再通知的顺序。
显式 VIDEO 控制与首个视频消息共用同一个内部视觉连接初始化函数，解码事件进入原有同一个 Realtime 会话。

```json
{
  "message": "chat",
  "sessionId": "vendor-session",
  "body": "{\"chatIndex\":\"chat-1\",...}"
}
```

`sessionId` 只是 vendor 关联值。每次 WebSocket 有独立 connection ID；握手创建 Agent Server
`thread_id`；旧 chat 合同对应原生 run。connection/session/thread/run/delivery 不得互相替代。

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

两种 control 握手均可在 body 中传入 `context` 对象，与 Studio 使用同一 `AssistantRunContext` schema。例如：

```json
{"number":"user-1","callType":"AUDIO","context":{"persona":"general","enable_memory":false}}
```

`persona` 只允许 `general`（通用助理）或 `call_assistant`（通话助理）；媒体省略时默认后者，Studio 默认前者。
其余公开配置（cwd、Memory、审批、压缩阈值）使用相同字段与校验；媒体 cwd 默认按握手用户派生。
非法 context 在创建 thread 前返回协议错误。握手配置保存在当前连接，后续每个 chat run 复用；重连需重新传入，
省略时重新采用默认值。无握手的 legacy lazy bind 路径采用媒体默认配置。角色不改变认证或媒体能力。

未启用 `audioStreamV1` 时，服务端发送成功 control ACK 后，立即在同一连接发送一次非 durable 问候：

```json
{
  "message": "chatResponse",
  "body": "{\"number\":\"user-1\",\"message\":{\"type\":\"BRIEF\",\"chatIndex\":\"greeting:media-...\",\"content\":{\"intentResult\":{\"description\":\"你好呀～～\",\"status\":\"SUCCESS\"}}},\"displayOnly\":false,\"display_only\":false,\"sequence\":1,\"final\":true}"
}
```

兼容 `assistantControlStart` 的连接也在 `assistantControlStartAck` 后发送同一问候。问候以 WebSocket
连接为粒度只发送一次，不创建 Agent run、不写入 Memory、不进入 durable 主动投递 Store，也不携带
`deliveryId` 或要求客户端 ACK。首帧直接为 `chat` 的旧 lazy bind 兼容路径没有成功 control ACK，因此不发送
连接问候。

兼容 `assistantControlStart`，其 user 位于 `userInfo.number`，成功响应为
`assistantControlStartAck` 和 `{"code":"OK"}`。legacy AR 链路中的后续 `chat.userNumber` 和
`video.userNumber` 沿用旧协议的独立号码口径，不强制与 `userInfo.number` 相等；native thread 和认证身份
仍以握手身份为准。现代 `assistantControl` 的后续媒体号码必须与 `number` 一致。同一连接不允许重复绑定握手。

为兼容 2026-08-13 前的 AR 媒体链路，连接首个业务消息直接为 `chat` 时，媒体入口使用该帧的
`sessionId/userNumber` lazy 创建 native thread，再按正常 chat 流程发送 `chatProgress` 和创建 run；该兼容
路径不具备 control 协商的客户端能力和 durable 主动投递能力。非开发者认证连接仍要求 `userNumber` 与
Agent Server `user.identity` 一致。

“完成 VIDEO 握手”的结构化定义是：服务端已成功校验并绑定首个 control 消息、创建该连接对应的 native
`thread_id`，且绑定的 `callType` 等于 `VIDEO`。它不要求已经收到第一帧。后续 chat run 不把握手模式写成
Studio Assistant context。
每次 chat 开始时，媒体入口还会在进程视觉模块中冻结当时的 video IDs 与严格窗口，并只把服务端签发的 opaque
capability token 放入 namespaced run metadata；token 按认证身份与 thread 校验，run 结束即撤销。当前
`media_graph_input()` 只投影 chat 文本，窗口摘要、`source=live_camera` block 和 capability 都不进入标准用户 message；
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
- 媒体 run 与网页、Studio 默认共用 `~/assistant_cqy/`；媒体客户端不能提交 cwd。
- 每项必须有 `speakerNumber`、`time`；当前 Graph 输入取最后一条非空 `speechContent`。
- `chatIndex` 关联 run，`deliveryId` 只关联本次媒体投递 ACK。
- 媒体入口以 `multitask_strategy=interrupt` 创建 native run。若同一 thread 的旧 chat run 仍处于
  pending/running/retrying，Agent Server 原子终止旧 run，保留其已提交 checkpoint，并把当前用户输入加入该
  thread 后继续；若旧 run 已进入终态，当前 run 正常从 thread 最新状态开始。媒体入口不扫描或拼接历史，
  不把新用户消息伪装成 `Command(resume)`。

收到请求后先发：

```json
{
  "message": "chatProgress",
  "body": "{\"chatIndex\":\"chat-1\",\"deliveryId\":\"delivery-...\",\"status\":\"PROCESSING\"}"
}
```

`stream=true` 时，Graph 原生 `messages/partial` 中新增的 assistant 文本会沿用媒体既有的
`chatResponse` 增量结构发送。模型发生可重试瞬时故障时，Graph 原生 `custom` 流中的 `model_retry` 事件另行投影为
`chatProgress(status=RETRYING)`，携带 attempt、下一次等待秒数和固定提示；它不进入 assistant 正文或 checkpoint：

```json
{
  "message": "chatResponse",
  "body": "{\"message\":{\"chatIndex\":\"chat-1\",\"content\":{\"intentResult\":{\"description\":\"我先查一下。\",\"status\":\"PROCESSING\"}}},\"displayOnly\":false,\"display_only\":false,\"sequence\":1,\"final\":false}"
}
```

旧媒体合同创建原生 run 时开启 `stream_subgraphs=true`，用于接收并过滤同步 task worker 的内部流。
当前 chat 禁用，恢复前须重新核对业务图流过滤。旧模型在 ToolCall 前生成的普通文本按上述结构
原样增量发送；若模型直接产生 ToolCall 而没有任何前导正文，媒体适配器不合成提示语，等待模型后续正文或
终态。ToolCall name/arguments、ToolMessage、原生 updates 与 custom Tool 生命周期均不进入媒体正文。

Graph 完成后发送成功终包：

```json
{
  "message": "chatResponse",
  "body": "{\"number\":\"user-1\",\"message\":{\"type\":\"BRIEF\",\"chatIndex\":\"chat-1\",\"content\":{\"intentResult\":{\"description\":\"回答\",\"status\":\"SUCCESS\"}}},\"displayOnly\":true,\"display_only\":true,\"sequence\":2,\"final\":true,\"deliveryId\":\"delivery-...\"}"
}
```

当前 wire 不发送或接受 `assistantMode`；请求 body 出现该字段时直接返回协议错误。媒体适配器只把 chat 文本机械转换为
标准 `HumanMessage` content，并把入口、视觉 capability 等受信运行事实放入服务端签发的 namespaced metadata；
公开 run context 提交经过同一 schema 校验的会话配置。`stream=true` 只投影
`AIMessageChunk` 的 string content 或 `text|output_text` block；当 `messages/metadata` 存在时，明确标记为
非 `model` 节点的 chunk 会被排除；metadata 缺失时按标准 assistant chunk 降级投影，避免原生流存在但媒体侧
只能收到终包。同步/异步 worker 等已标记的内部子图文本、tool-call name/arguments、ToolMessage 和 updates 不进入媒体正文。
Agent Server 的 `messages/partial` 是同一 message 的累计快照，适配器按 message ID
计算 append-only delta；模型生成的 Tool 前导文本与工具后的下一条 assistant message 之间若均无换行，适配器在新消息首包
补一个换行。中间包按 `sequence` 递增且 `final=false`，不携带 `deliveryId`；`stream=false` 不发送中间包。

统一 Agent 顶层主循环的原生 dynamic prompt middleware 根据 `context.persona` 选择策略。通话助理要求工具前导最多一句、
最终回复通常一到三句且保持口语化；同一 model-call 边界设置 `max_tokens=128`。通用助理使用通用提示词与默认输出上限，
两个入口均可选择任一角色。内部角色不产生媒体正文，固定使用通用策略；媒体 custom route 不持有或拼接任何提示词。

最终正文仍来自 terminal values 中的最新标准 `AIMessage`，适配器不把 delta 拼接成业务终态。成功终包发送该
最新消息中尚未流出的后缀，使用最后一个 `sequence`、`final=true`，并独占 `deliveryId`；若最终文本与已流出的
最新 assistant message 不一致，则完整发送权威终态，避免丢失正文。已发送中间包且终包不含媒体 detail 时，
终包的 `displayOnly/display_only=true`；citation 的 `fullDescription` 始终保留完整权威终态。

若当前用户轮次存在成功 Tool 的标准 `ToolMessage`，媒体终态会从其 artifact 内严格校验的
`assistant_agent_delivery_v1` 确定性追加用户可见文本或受管生成物引用，而不是要求模型复述。例如购物 Tool
可声明兼容卡片块：

```text
<detail>
1. 京东 - 商品名 2599元 <link>https://...</link><pic>https://...</pic>
</detail>
```

Tool 负责从已校验的领域结果构造卡片、导航链接或 `output_refs`；媒体 Runtime 不识别购物、酒店、AMap 或
图片 Tool 名，也不导入其领域 model。它只查看最后一个 HumanMessage 之后的消息，按 `ToolMessage.name`
保留最后一条；如果该条失败，不回退同名旧结果。标准 `type=file` block 可通用交付安全 HTTP(S)
下载链接。所有文本、URL 和 refs 均有界、去重；Graph 拓扑、ToolNode 和 checkpoint 生命周期不变。

媒体客户端只关闭 WebSocket 表示会话结束，不新增挂断消息。媒体入口释放内部 Gateway
连接，由其统一 `onSessionClosed` 消费原生 ConversationSync 的本批用户记录；直接 Gateway 网页文字和语音也走同一 hook。媒体层不另建记忆收集器。
同一进程内每会话只提交一次，媒体层不额外提交。网络断线与主动挂断无法区分，均作为结束；重连是新会话。
提交使用官方 `RemoteReflectionExecutor.submit(after_seconds=0)` 在 companion Memory thread
立即运行提取图，业务图与转写事件均不调度提取；没有 30 分钟延迟或静默计时器。

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

主动消息由显式产品 publisher 写入 durable Store，不经过当前 `AssistantAgent` run。媒体连接按 native
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

未启用前台 `audioStreamV1` 且 `REALTIME_VISUAL_ENABLED=false`（默认）时 VIDEO 握手仍可成功，视频消息通过身份与字段校验后返回
`videoResponse` / `{"code":0,"message":"realtime visual disabled"}`，不进入实时解码或感知队列。
若独立启用了远端视觉记忆归档，归档仍按原配置接收已校验数据。此开关不影响 audio 或业务 Graph。
前台 `VIDEO + audioStreamV1` 直接桥接至 Realtime 模型，见 5.2；后台窗口冻结及视觉工具能力仅在设置
`REALTIME_VISUAL_ENABLED=true` 并重启后启用。

未启用 `audioStreamV1` 的 `audio` 继续只做传输层 ACK，不把原始音频写入 Graph State。`video` 先在 WebSocket 热路径完成
字段校验，再进入连接级 `one in-flight + one latest pending` 后台解码边界；新 pending 会替换尚未开始的旧
pending，旧消息仍收到正常 `videoResponse`，但不再解码、提交 VLM 或进入视觉窗口。后台任务在 media edge
的工作线程中把独立 Annex-B H.264 frame 解码为有界 JPEG window，因此消费速度下降时不会形成历史 FIFO
积压。稳定 `video_id` 仅保留在进程视觉 owner 和 namespaced capability facts 中，当前不进入 Graph 标准 message；
收到首个视频 ID 后，受治理 `live_view_inspect` Tool 通过 run-scoped capability 暴露，并从进程级有界内存 frame index 读取。H.264 hex、JPEG
正文和本地路径均不进入 Graph State、prompt 或 Agent Server Store。

显式启用远端视觉 Memory Service 时，同一批已校验 H.264 bytes 还会复制到独立顺序归档 lane；该 lane
不复用实时视觉 latest-wins 队列。它每 30 秒或连接关闭时 remux 为 MP4，通过 Agent Server custom app 的
短期 capability URL 供内网 Memory Service 拉取，并异步调用 `/v1/media/upload` 与 `/v1/tasks_status`。
上传、轮询或归档失败只降级历史视觉记忆，不改变视频 ACK、实时视觉和 native chat run。
归档按整条 video message 原子 admission；连接数、待处理 item 或 byte 预算不足时整批不进入归档 lane，但仍按
既有实时路径处理并返回视频 ACK，不允许部分归档后依赖客户端重试。

视频热路径使用同一个安全 `video_index`，并在完成解码后增加服务端 `sequence`，依次记录
`media_video_websocket_received`、`media_video_ingestion_dequeued`、`media_video_decode_started`、
`media_video_decode_finished`、`media_video_context_indexed`、`media_video_semantic_submitted` 和
`media_video_semantic_admitted`。日志自身时间戳是对应边界的服务端 wall time；字段中的
`queue_wait_ms`、`decode_ms`、`index_ms`、`receive_to_submit_ms`、`submit_ms` 和
`receive_to_admit_ms` 使用同一进程 monotonic clock 计算。`captured_at_ms` 仍只表示媒体包
`contents[].time` 提供的上游帧时间，不得当作服务端收包或解码时间。日志不记录 H.264/JPEG 正文、路径、
用户正文或 Provider payload；不满足安全字符约束的 `video_index` 只记录摘要。

用户主动上传的图片或视频从 Studio/普通 Agent Server 入口以 LangChain 标准 content block 进入；标准块缺少
`source` 时按主动上传处理，兼容入口也可显式标记 `source=uploaded`，统一交给独立的
`uploaded_media_inspect`。本 WebSocket chat wire 仍只投影文本，不承载静态附件；进程视觉模块仍冻结 live camera
窗口和 namespaced capability facts，也会继续运行
selector、并行 VLM 和 reminder manager；但当前 custom route 不向标准 message 注入 `source=live_camera` block。
统一条件 middleware 只解析服务端签发的 capability 和冻结投影：有视频 ID 时暴露
`live_view_inspect` 与 `visual_reminder_manage`，同时存在目标序号和可检索历史时暴露 `visual_memory_search`。

媒体 wire 只负责把每个成功解码帧提交给连接级视觉句柄。chat 到达 A 时刻后，入口在任何异步发送或其他
`await` 之前同步冻结 selector 已经登记的当前半固定关键帧窗口（1～5 帧），随后立即发送 `chatProgress`，
不等待当前 H.264 解码、SigLIP2 embedding、VLM 或未来关键帧。冻结窗口最后一帧作为 target；窗口关闭后
异步启动多图 VLM，下一 selected
关键帧从新窗口开始。用户输入是否最终调用视觉 Tool 不改变这个短期记忆分段动作。当时仍处于
pending/in-flight、尚未 selected 的帧不属于已关闭窗口。尚未完成解码或尚未完成选帧的工作不会阻塞 run
创建；其后成为新关键帧也不回写已经冻结的本轮投影。
随后媒体入口一次把冻结投影保存在进程 owner 并将 opaque capability token 写入 namespaced metadata，
不传 JPEG、Provider client 或 task，也不把 `window_id`、`window_start_sequence` 和 `target_sequence` 写入标准 message。
窗口并发、semantic keyframe、目标帧等待、ready/missing 结果和
晚到帧处理均以 [`visual-perception-architecture.md`](visual-perception-architecture.md) 为唯一权威。
主 LLM 只能看到最终可见 Tool 和对应动态规则，不能看到 video ID、sequence 或其他投影内部字段。

保留 callback：

```text
POST /calling-agent-service/v1/{job_or_session_id}/{chat_index}/3d-gen-back
```

它记录中性 3D job artifact；不执行 Assistant Graph。在线连接以 native `thread_id` 订阅完成事件，并机械
投影为带 `TD_MODEL/VIDEO/IMAGE` detail 的 `chatResponse`。当前 hub 是进程内在线交付，不是 Agent Server
run/checkpoint，也不宣称跨进程、离线或 exactly-once 投递；没有 job 且没有在线 subscriber 时 callback
返回失败，让上游决定是否重试。

### 5.1 V2 双向音频 `audioStreamV1`

此扩展复用 `8081/agent-service/v1` WebSocket 和 JSON-string `body`。媒体发送：

```json
{"message":"assistantControl","sessionId":"call-1","body":"{\"number\":\"user-1\",\"callType\":\"AUDIO\",\"clientCapabilities\":{\"audioStreamV1\":true}}"}
```

Gateway 媒体适配器先连接本进程的 `ws://127.0.0.1:3101/api/realtime`，收到真实 `voice.ready` 后返回 control ACK 与
`audioReady`。不发送旧固定 TTS 问候。`audioReady.body`：

```json
{"version":1,"input":{"codec":"pcm_s16le","sampleRate":16000,"channels":1,"encoding":"hex"},"output":{"codec":"pcm_s16le","sampleRate":24000,"channels":1,"encoding":"base64"}}
```

需要 V2 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 及已显式启用真实能力的 Gateway。当前只支持本机 personal
Gateway 的 `local-developer` 认证身份，不把媒体 `number` 伪装成 Gateway 多用户身份；业务和 Memory 使用
Gateway 已配置的个人身份。Gateway 如配置了其他业务身份，不应启用此扩展。浏览器与媒体可同时运行；
每条媒体连接使用服务端随机生成的独立 Gateway session ID，连接和语音租约按 owner + session 隔离。
同一会话的重复连接仍受占用规则保护，媒体关闭不影响网页会话。任务通知只自动交付来源会话。

8081 必须显式协商 `audioStreamV1`；未协商会返回明确失败，不再静默进入仅 ACK 模式。
媒体 `context` 业务配置不应用于前台模型，8081 拒绝非空 `context`，前台配置沿用 Gateway。
8088 的旧入口暂时兼容原行为，迁移期间不能同时把同一媒体会话发往两个端口。
VIDEO 握手还建立本机 `/internal/media-visual/v1` 连接，在复用既有视频处理的同时，
把 `gatewayVisualEvent` 内的 JPEG/画面状态事件转入前台 GCP 会话。这些内部事件不发给媒体服务。
内部视觉连接失败时明确关闭媒体实时连接；`videoClear` 清除画面缓冲并返回 `videoResponse`。

**上行：** 保留 `audio` 消息和 `contents[].audioContent` 十六进制字段。必须声明
`audioConfig={"codec":"pcm_s16le","sampleRate":16000,"channels":1}`，无 WAV/RTP 头、16-bit little-endian。
`contents` 1～10 项，总量每消息不超过 32000 bytes（1 秒），推荐 20～100ms 一包，按采集节奏持续发送静音与讲话。
`userNumber` 必须与现代 control 握手一致。错误标注的 `opus` 明确拒绝，不自动猜格式或转码。
合法音频发送到 Gateway 后 ACK 为 `audioResponse` / `{"code":0,"message":"audio forwarded"}`，
不表示 ASR 成功。单独的 `chat` ASR 只返回 `chatAck`，不再次驱动模型，避免同一句话回答两遍。

**下行：** 下表 `body` 均为解开 JSON string 后的对象；外层 `sessionId` 沿用 control。

| message | body 字段 | 语义 |
| --- | --- | --- |
| `asrResult` | `turnId`, `text`, `final:true` | 模型侧用户输入音频的最终 ASR，仅用于用户字幕展示 |
| `audioStart` | `taskId`, `streamId`, `codec:"pcm_s16le"`, `sampleRate:24000`, `channels:1`, `encoding:"base64"` | 首个分片前声明格式 |
| `audioChunk` | `taskId`, `streamId`, `sequence`, `audioContent` | Base64 原始 PCM；sequence 从 0 连续递增；每片最多 4800 bytes / 100ms |
| `audioEnd` | `taskId`, `streamId`, `lastSequence` | 发送结束，不代表 APP 播放完成 |
| `audioInterrupt` | `taskId`, `streamId`, `reason` | 立即停止对应流、清空播放队列并丢弃迟到包；reason 为 `NEW_SPEECH`、`MEDIA_INTERRUPT` 或 `GATEWAY_ERROR` |
| `audioError` | `code`, `message` | 连接不可继续时显式失败，媒体关闭连接并按自身重连策略重新握手；不补播旧流 |

`taskId="voice:<Gateway responseId>"`，`streamId="audio:<Gateway responseId>"`。这是语音回复关联 ID，
不是业务委派 task/run ID。每条回复独立编号，媒体应按当前 WebSocket + streamId 隔离队列。
`audioEnd` 后仍可能收到打断（发送结束但尚未播放完）。Agent 保留最多 16 个等待播放回执的流，
超限明确失败；慢接收端写入超过 5 秒也结束音频链路，不静默丢音频。

示例（两个零值采样，仅用于说明格式）：

```json
{"message":"audioChunk","sessionId":"call-1","body":"{\"taskId\":\"voice:r1\",\"streamId\":\"audio:r1\",\"sequence\":0,\"audioContent\":\"AAAAAA==\"}"}
```

**媒体必须回传 `audioStatus`：** body 携带原样的 `taskId`、`streamId` 和 `status`：

- `accepted`：已建立播放队列，可选。
- `playing`：真正开始播放，映射为 Gateway `playback.started`，随后 Gateway 可发布字幕。
- `completed`：收到 `audioEnd` 且该流播放队列已排空，映射 `playback.ended`；不能在收包时提前回传。
- `interrupted`：该流已停止并清空，映射 `playback.cancelled`。
- `failed`：不能播放，映射 `playback.cancelled`；可附媒体自己的 `code`、`message`，Agent 不记录原始错误正文。

终态回执可重复，未知 streamId 或 taskId 不匹配返回协议错误。媒体收到 interrupt 后不得再报告 completed。
`interrupt` 上行沿用原名，既取消实时回复也发出 `audioInterrupt`，随后仍返回原 interrupt ACK。
断线时媒体必须自行清空全部音频队列；Agent 关闭对应 Gateway 会话，不保存或重放音频。

**用户 ASR：** Gateway 媒体适配器（3101/8081）将当前连接收到的
`transcript.final(role=user)` 转为 `asrResult`，沿用外层 `sessionId` 和 JSON-string `body`：

```json
{"message":"asrResult","sessionId":"call-1","body":"{\"turnId\":\"turn-1\",\"text\":\"帮我查一下天气\",\"final\":true}"}
```

`turnId` 原样使用 Gateway 用户轮次 ID，不使用 assistant 的 responseId/taskId。
仅转发具有非空字符串 `turnId` 和非空文本的最终结果，文本原样保留；不转发增量或失败事件。
媒体端按当前连接与 `turnId` 展示或更新用户字幕，不触发 TTS，也不将其作为新的 `chat` 输入回传模型。
不新增 ACK 或断线重投；用户 ASR 不依赖助手音频播放回执。旧 8088 入口不提供此下行扩展。

**文本与重复 TTS：** 模型最终字幕仍使用 `chatResponse`，`message.chatIndex=taskId`，同时附带顶层
`taskId`、`streamId`、`audioSource:"model"`、`displayOnly:true`、`display_only:true`、`final:true`。
媒体只显示该字幕，绝不能再触发自己的 TTS。文本正文为完整最终字幕，`sequence=1`；本扩展不转发字幕增量。
已有非模型音频文本消息规则保持不变。

媒体服务负责 PCM 缓冲、必要的重采样和编码为 WebRTC 协商的 Opus/PCMU/PCMA，再发送到 APP；
本接口不传 RTP 头、SDP、SSRC，也不声称 socket 发送成功等于 APP 已播放。
24kHz 输出与当前锁定 Gateway DashScope adapter 一致，参考
[百炼服务端事件文档](https://www.alibabacloud.com/help/en/model-studio/server-events)。

### 5.2 前台原生 Realtime 视频上下文

媒体握手声明 `callType="VIDEO"` 和 `clientCapabilities.audioStreamV1=true` 后，现有音频桥同时向 Gateway
协商 `input.image_buffer` 与 `client.events`。继续发送原有 `video` 消息：`videoConfig.codec="H264"`，
`contents[].videoContent` 为带 Annex-B 起始码、可独立解码的 H.264 十六进制帧，`time` 使用 ISO 8601 时间。
身份、个人 Gateway 占用和真实模式门槛与 5.1 相同；没有该音频会话时不会自动创建另一条模型连接。

数据路径：媒体 H.264 → 连接级 latest-wins 解码 → JPEG → 同一 Gateway 会话的
`input_image_buffer.append` → DashScope Realtime。图片最长边界为 1280×720，Base64 不超过 256 KiB，
桥接最多每秒一张，Gateway 还会校验尺寸和时间顺序并限流。音频接收不等待视频解码；视频队列最多一个
执行中任务和一个最新 pending。多个输入帧、音频和响应通过同一 WebSocket 的发送顺序关联，
`occurred_at` 只供 Gateway 排除旧帧，不作为模型 API 的音画精确对齐时间戳。

`REALTIME_VISUAL_ENABLED=false` 不阻止此路径；后台开启时复用解码结果提交原有感知流水线。
关闭后端视觉且未启用远端归档时，只保留连接级有界 JPEG 窗口，断线清理，不写 Graph State、Memory 或视觉索引。
`videoResponse` 成功只表示本地处理/提交，超频帧和被替换的 pending 可以跳过；不是 Provider 已理解的确认。

**上下文归属：** Python 服务不先生成画面文字；Gateway 通过图片事件提交原生多模态输入。
图片与音频分别追加到同一个 Provider 会话，在 VAD 轮次结束时由 Provider 提交输入并生成回复；桥不会逐帧
触发 `response.create`。发送图片前如果尚无音频，现有 Gateway adapter 会补 20ms PCM 静音建立输入时间线。
模型在其会话上下文内结合图像、语音和既有对话回答；具体图片编码、上下文淘汰和跨轮保留由 Provider 实现，
本地不承诺“每帧永久保留”或“只看最后一帧”。参考[官方客户端事件](https://help.aliyun.com/en/model-studio/client-events)。

初始无画面、首次成功提交图片、连续 3 秒无新提交图片时，桥通过 `client.event.publish` 的 `context`
方式同步视觉状态，不主动请求模型回答。超时会发送 GCP `input_image_buffer.clear`，下一张有效帧可恢复。
此清空只清理 Gateway 待发图片及限流状态；当前 DashScope adapter 没有远端图片历史清空操作。
停止视频不代表已经提交的历史图片从模型上下文消失，状态文本明确旧画面不能当作当前环境。
断线关闭此 Realtime 连接，不重放历史 JPEG；该路径不提供跨会话视觉记忆。

## 6. 错误与认证

- 非 `v1` 连接返回失败并以 1008 关闭。
- 非法 JSON body、缺字段、identity mismatch、重复关联或未知消息返回结构化失败 envelope。
- custom route 启用 Agent Server auth hook。内部 thread/run 调用走同源公开 API 并转发 identity header；
  Graph、Memory 与 Tool 只读取 Agent Server 注入的 `user.identity`，不得使用 `/noauth` 或信任客户端 Context 身份。
- mock 与 real mode 都使用 tokenless developer principal；`X-Assistant-User` 存在时直接作为 identity，省略时
  使用 `local-developer`。资源 owner 继续按该 identity 隔离，但 identity 未经密钥验证，因此服务端口不得暴露给
  不受信网络。

## 7. 重连与当前限制

相同 `assistant graph ID + user + vendor sessionId` 通过确定性 UUID 映射到同一个 native thread；同一
`assistant-business-v2` connection 重连不会创建第二份对话轴，且 业务 UUID 不会碰撞旧版 native v1/v2/v3/v4 UUID。
创建请求以 `if_exists="do_nothing"` 返回 existing thread 时，中央 SDK 边界仍会验证 Agent Server 原生 metadata
`graph_id=assistant-business-v2`；retired native v1/v2/v3/v4、voice-v1、worker-v1 或缺失字段的 thread 在 session bind
和 run 创建前拒绝。
custom route 创建 run 时使用 `stream_resumable=true` 与 `on_disconnect=continue`，内部订阅临时断开后从最后
event ID 调用 `threads.join_stream`，而不是重建项目自有 session/runtime。同一连接的重复 `chatIndex` 在创建
第二个 run 前拒绝；后续不同 chat 使用 Agent Server 原生 `interrupt`，避免旧的 pending/retrying run 永久阻塞
新用户轮次。

媒体 WebSocket 断开时仍 best-effort cancel 该连接的活动 reactive run。主动 durable 行释放 connection lease，
相同 `user + vendor sessionId` 重连到同一 native thread 后重发未 ACK 行；reactive chat 的既有终包仍只由
当前连接关联，不纳入主动 Outbox。Agent Server stream resume 解决执行事件订阅恢复，与媒体 delivery
outbox 保持两套不同语义。当前主动 Store 是单实例/共享持久卷 SQLite；多主机共享事务实现、周期 progress、
durable task 生产者尚未接入该主动 Outbox；后台视觉 observer 只发布视觉语义记录，不伪装 proactive
producer。citation、生成图片 detail、H.264 显式
视觉引用和在线 3D artifact 投影已支持。

生成图片使用标准 `ToolMessage(content, artifact)` 双通道，公共 Graph 不改写最终 `AIMessage`。媒体入口从
当前用户轮次成功的 `image_generation` ToolMessage 中优先读取 `artifact.images[].output_ref`；对旧 checkpoint
兼容读取 `download_urls` / `output_ref`。新写入 checkpoint 的唯一稳定引用为
`/artifacts/{thread_ref}/generated/{filename}`；媒体 resolver 按原生 Store 登记读取 `<cwd>/outputs/` 内唯一的正式文件，
校验 owner/thread、路径、大小和媒体类型后投影为 `intentResult.detail[].type=IMAGE` 的 Base64 正文。
旧 `artifact://v1/...` 语法仍可解析，但必须有相应 Store 登记；不扫描旧磁盘目录补全引用。
正式文件不做 thread TTL 回收；Base64 只存在于媒体 wire，不写回 Graph State。
Studio 当前只显示最终文本，不承诺渲染 Tool artifact。

媒体连接只关闭自己取得的视觉 session handle，不关闭 Agent Server 进程级视觉 owner；完整资源所有权和
清理规则见视觉 authority 与 Agent Server authority。

## 8. 验证

```bash
python -m compileall -q src/assistant_agent/agent_server/media_app.py src/assistant_agent/agent_server/media_protocol.py
```

以上仅检查语法；行为变更的临时测试按 `tests/README.md` 显式运行。
