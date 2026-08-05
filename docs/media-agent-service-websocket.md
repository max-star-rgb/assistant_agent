# Media-Agent WebSocket 接口权威文档

Last updated: 2026-08-05

本文档是以下当前接口及责任边界的唯一权威文档：

- Media-Agent 与 `assistant_agent` 之间的 `/agent-service/v1` WebSocket；
- Agent 生成图片经 Media-Agent 转交渲染服务的 `chatResponse` 数据契约；
- `image_to_3d` 向 3D 服务提交图片的 HTTP 接口；
- 3D 服务回调 Agent、Agent 保存中性任务结果以及按入口能力选择媒体投递的 HTTP/WebSocket 接口。

本文合并了旧临时 Mock Agent 协议说明和旧 H.264 视频传输专项说明。媒体侧协议为外部对接基准；
Agent 侧负责兼容该协议，并在内部把 `chat` 文本请求转入 Gateway 和 assistant runtime。

事实来源分级如下：

- Agent WebSocket、3D 提交和 3D callback 以本仓库当前源码、配置和专项测试为准；
- Media-Agent 内部 `RenderingClient -> /rendering/v1/torender` 属于仓库外部署契约，本仓库只记录
  已提供的对接结构和责任边界，不把它描述为 Agent 自身实现；
- 本文“当前实现”描述现状；进程内任务存储、旧 callback URL 兼容等限制会明确标注。

不要再新增并行的 Media-Agent 接口文档；需要变更 wire 字段、流式语义、H.264 约束、联调命令或验收证据时，更新本文档，并按需同步 `docs/gateway-architecture.md` 中的 Gateway 边界摘要。

## 1. 连接信息

- 协议：WebSocket
- 联调默认端口：`8089`
- URL：`ws://<agent_host>:8089/agent-service/v1`

本仓库本地服务默认端口是 `8000`。联调媒体服务时可用 `scripts/run_server.py --port 8089` 对齐上述端口。
该协议只要求遵循本文档的 JSON envelope，可由任意语言实现。

## 2. 消息格式

所有消息采用 JSON 文本帧，统一包装如下：

```json
{
  "message": "<消息类型>",
  "body": "<JSON字符串>"
}
```

`body` 必须是 JSON 字符串。也就是说，外层 `body` 字段的类型是 string，Agent 收到后再反序列化为对应消息体 object。

Agent 兼容说明：

- 每条 WebSocket 连接都会生成独立的内部 `agent-service-*` Gateway/runtime session id；外层
  `sessionId` 不替代这个内部 ID。
- 如果外层带 `sessionId`，Agent 把它作为 vendor 协议关联值保存并在响应中回传。
- 如果外层不带 `sessionId`，Agent 从 `assistantControl.body.number`、`chat.body.userNumber`、
  `audio.body.userNumber`、`video.body.userNumber` 或 `interrupt.body.number` 派生协议关联值，响应默认
  不额外增加外层 `sessionId`。
- Agent 仍兼容旧的 `assistantControlStart` / `assistantControlStartAck` 握手，但真实媒体协议优先使用本文档的 `assistantControl`。

## 3. 消息类型定义

### 3.1 客户端 -> Agent

| message 类型 | 说明 | body 结构 |
| --- | --- | --- |
| `assistantControl` | 建立连接时发送 | 参见 4.1 |
| `chat` | 文本/图像消息 | 参见 4.2 |
| `audio` | 音频帧 | 参见 4.3 |
| `video` | 视频帧 | 参见 4.4 |
| `interrupt` | 打断消息 | 参见 4.5 |
| `chatResponseAck` | 媒体侧确认成功终包已经应用 | 参见 4.2 |

### 3.2 Agent -> 客户端

| message 类型 | 说明 | body 结构 |
| --- | --- | --- |
| `assistantControl` | 连接响应 | 参见 4.1 |
| `chatResponse` | 文本响应及 IMAGE、TD_MODEL、VIDEO 渲染数据 | 参见 4.2、4.6 |
| `audioResponse` | 音频响应 | 参见 4.3 |
| `videoResponse` | 视频响应 | 参见 4.4 |
| `interrupt` | 打断响应 | 参见 4.5 |
| `chatProgress` | 可选的长任务处理中状态 | 参见 4.2 |
| `chatResponseAck` | 最终响应应用层确认 | 参见 4.2 |
| `error` | 未知消息或无法归属到具体 handler 的协议错误 | 参见 5 |

## 4. 消息体详细结构

### 4.1 assistantControl

发送 `client -> agent`：

```json
{
  "number": "用户号码",
  "callType": "AUDIO",
  "modelName": "模型名称(可选)",
  "language": "zh-CN",
  "clientCapabilities": {"chatProgress": true, "chatResponseAck": true},
  "clientInfo": {"clientType": "run_client", "clientName": "scripts/run_client.py"}
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `number` | string | 是 | 用户号码；作为 Gateway `user_id`，不作为可恢复的内部会话 id |
| `callType` | string | 是 | `AUDIO` 或 `VIDEO` |
| `modelName` | string | 否 | 媒体侧希望使用的模型名称；Agent 当前记录但不直接用它绕过 provider/runtime policy |
| `language` / `locale` | string | 否 | 3D callback 通知语言；只识别 `zh`、`en` 及其区域后缀，缺省或其他值按 `zh` |
| `clientCapabilities.chatProgress` | boolean | 否 | 为 true 时立即并每 15 秒发送 `chatProgress` |
| `clientCapabilities.chatResponseAck` | boolean | 否 | 为 true 时最终响应携带 `deliveryId`，媒体处理后回 ACK |
| `clientInfo.clientType` | string | 否 | 仅用于安全观测分类；本地 `scripts/run_client.py` 发送 `run_client`，真实媒体可省略，缺省记为 `media_agent` |
| `clientInfo.clientName` | string | 否 | 仅用于安全观测；当前只记录已知的 `scripts/run_client.py` 本地调试客户端 |

Agent 每个 WebSocket 连接都会分配新的内部 `agent-service-*` Gateway session。
外层 `sessionId` 只作为媒体协议关联值原样回传，不用于恢复旧通话历史。
`clientInfo` 不参与 profile、provider、tool visibility 或安全策略选择。

旧兼容握手 `assistantControlStart` 的最小 body 为：

```json
{
  "userInfo": {"number": "用户号码"},
  "agentInfo": {"agentNumber": "Agent号码"},
  "language": "zh-CN"
}
```

成功响应 message 为 `assistantControlStartAck`，body 为 `{"code":"OK"}`。该兼容握手不协商
`chatProgress` 或 `chatResponseAck`；新接入应使用 `assistantControl`。

响应 `agent -> client`：

```json
{
  "code": 0,
  "message": "success",
  "phoneNumber": "用户号码"
}
```

外层示例：

```json
{
  "message": "assistantControl",
  "body": "{\"code\":0,\"message\":\"success\",\"phoneNumber\":\"10086\"}"
}
```

### 4.2 chat

发送 `client -> agent`：

```json
{
  "chatIndex": "对话索引",
  "userNumber": "用户号码",
  "contents": [
    {
      "speakerNumber": "说话人号码",
      "speechContent": "文本内容",
      "time": "ISO时间戳"
    },
    {
      "speakerNumber": "说话人号码",
      "imageContent": "图片Base64(可选)",
      "time": "ISO时间戳"
    }
  ],
  "stream": true
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `chatIndex` | string/number | 是 | 对话索引，响应中原样返回 |
| `userNumber` | string | 是 | 用户号码；作为 Gateway `user_id`，内部 session 由本连接的 `agent-service-*` 标识承担 |
| `contents` | array | 是 | 至少一条内容 |
| `contents[].speakerNumber` | string | 是 | 说话人号码 |
| `contents[].speechContent` | string | 文本消息必填 | 已完成 ASR 的文本 |
| `contents[].imageContent` | string | 图片消息可选 | 图片 Base64；当前作为媒体兼容字段接收，不直接进入图像 provider |
| `contents[].time` | string | 是 | ISO 8601 时间戳 |
| `stream` | boolean | 否 | 为 `true` 时，真实 Provider token delta 投影为多个 `chatResponse` 中间包，之后发送一个成功终包关闭本轮 stream；缺省或 `false` 时只发送终包 |

处理规则：

- Agent 使用最新一条非空 `speechContent` 作为本轮 Gateway 输入文本。
- 只包含 `imageContent` 的内容项可以随请求传入，但当前不单独触发图像理解。
- `chat` 会进入 `GatewayTurnFacade -> GatewaySessionManager -> GatewayRuntimeAdapter -> AssistantRuntimeApp -> AgentGraphRuntime`。
- 每个媒体 WebSocket 拥有一个连接级逻辑 AgentSession（本地
  `GatewaySessionManager/GatewaySessionService`）；连接清理会取消 turn 并销毁该
  AgentSession，但不会关闭进程共享的 `AssistantRuntimeApp/AgentGraphRuntime` 执行引擎。
- Langfuse 的 `langfuse.session.id` 使用这个内部 `agent-service-*` AgentSession id，
  并标记 `session_scope=agent_service_connection`；`langfuse.user.id` 使用
  `userNumber`。外层 vendor `sessionId`、`chatIndex`、Gateway `turn_id/run_id` 和
  delivery id 仍是不同的关联标识，不作为 Langfuse Session 分组键。
- chat run 在独立任务中执行，WebSocket 主循环会继续接收并 ACK 后续媒体消息。
- 若 WebSocket 在 chat run 执行期间异常断开，Agent 会记录 ERROR 级安全日志，
  并对本轮 Gateway run 发送 `run.cancel`（`source=gateway_disconnect`、
  `reason=client_disconnected`）；日志只包含脱敏 session 摘要、close code、
  reason 是否存在和计数，不记录原始 reason 或用户文本。
- 上述行为是当前 vendor `/agent-service/v1` 的连接级契约。规范化
  `/ws/gateway` 已支持 `DETACHED` grace、`delivery_cursor` 和
  `session.resume` 有界重放，但 vendor 协议尚未提供安全的跨连接 resume identity/cursor，
  因而不能把旧媒体连接的内部 session 或 chat 输出自动转移到新连接。
- `stream=true` 的中间包只携带本包新增文本，`status=PROCESSING`、`sequence>=1`、`final=false`；终包只携带尚未发送过的剩余文本，`status=SUCCESS`、最后一个 `sequence`、`final=true`。
- 若本轮正文已经全部通过中间包发送，成功终包的 `description` 为空字符串。媒体/App 可以继续按增量追加处理，不会重复追加完整答案。
- 若本轮已经发送过中间包且终包不含渲染 `detail`，成功终包仍会同时携带
  `display_only=true` 和 camelCase 兼容字段 `displayOnly=true`，供支持该标记的客户端识别纯文本
  终包。含 `IMAGE detail` 时改用媒体 legacy 完整结构，只携带 `display_only=false`，不携带
  `displayOnly/sequence/final/deliveryId`。
- 只有真实 Provider token delta 产生中间包；Provider 不支持或未产生 token delta 时，即使 `stream=true` 也只发送一个完整终包，不伪造流式能力。
- Provider text delta 是 append-only provisional 输出：即使同一 Provider turn 后续出现
  native tool call，已经发送的 text 也不会撤回；tool-call name/argument delta 不发送给媒体，
  只在 Runtime 内部累积完整后进入工具治理。工具执行后的下一轮 Provider text 继续按 sequence
  追加。若工具前导文本与下一轮正文之间尚无换行，下一轮首个 delta 会带一个前置 `\n`；
  任一侧已有换行时不会重复生成。媒体侧不得把 `PROCESSING` 中间包视为可回滚内容。
- Gateway `run.end.payload.response_text` 携带 Runtime 归一化最终正文。Agent-Service
  使用该终态字段计算成功终包，而不是把 provisional `stream.chunk` 拼接成最终答案；
  因此 `stream=false` 仍只返回规范化终态正文，截断、错误恢复文案和购物 detail 也可在终包补齐。
- 购物推荐/比价遵循 ReAct：`shopping_search` 返回结构化结果，下一轮 LLM 消费不含链接和展示模板的精简 observation，生成正常自然语言。由于 Agent-Service 声明 `supports_shopping_detail_v1=true`，Gateway Runtime adapter 会从完整成功 ToolResult 抽取标题、平台、价格、商品链接和图片链接，把唯一 `<detail>...</detail>` 追加到最终交付正文；不覆盖 `AgentResponse.message` 或 conversation history，也不增加 LLM 调用。若自然语言已通过 Provider token delta 发送，终包 `description` 只携带尚未发送的换行和协议块。
- `deliveryId` 和 `chatResponseAck` 只属于成功终包；中间包和失败终包都不进入应用层 ACK 状态。
- 生成图片不走独立媒体上传接口。媒体服务建立 `/agent-service/v1` 连接后，Agent 复用同一
  WebSocket 发送 `chatResponse`；媒体服务再将 `message.content.intentResult.detail[]` 转发给
  渲染服务。图片项固定为
  `{"type":"IMAGE","imageId":"<图片ID>","image":"<纯Base64>"}`，不携带 Data URL 前缀。
  媒体/渲染侧通过 `intentResult.detail[].type="IMAGE"` 识别图片渲染数据；这是渲染内容的业务
  判别字段。图片包设置 `display_only=false` 作为展示兼容标志，但该标志不是媒体到渲染的类型路由
  依据。
- Agent 图片生成成功后，Gateway `run.end.payload.output_refs` 保留最多 4 个去重后的输出引用。
  Agent-Service 只读取本 Agent 托管的 `/artifacts/generated/` 图片，并在成功终包中投影为
  `intentResult.detail`；不会把 Provider 临时 URL、本地绝对路径或任意外部引用直接发送给媒体。
- 图片原始文件必须不超过 25 MiB，并且内容可识别为 JPEG、PNG、GIF 或 WebP。
  `imageId` 使用 Agent 托管 artifact 文件名去掉扩展名后的图片 ID；找不到、超限、越界或无法识别的引用会被忽略，
  不得让已有文本响应失败。
- `detail` 只在运行成功后发送；流式 `PROCESSING` 中间包、`chatProgress` 和失败包不携带图片，
  避免 Base64 重复发送。图片使用媒体 legacy 完整结构，不加入 `deliveryId` ACK 扩展。

协商 `chatProgress` 后，Agent 立即并每 15 秒发送一次：

```json
{"message":"chatProgress","body":"{\"chatIndex\":\"chat-1\",\"deliveryId\":\"delivery_xxx\",\"status\":\"PROCESSING\"}"}
```

协商 `chatResponseAck` 后，最终响应增加 `deliveryId`。媒体处理完成后发送：

```json
{"message":"chatResponseAck","body":"{\"deliveryId\":\"delivery_xxx\",\"chatIndex\":\"chat-1\"}"}
```

Agent 返回 `chatResponseAck` 且 `code=0` 才表示应用层 ACK 已记录。媒体端对视频理解 turn 的等待时间必须至少为 90 秒。

Agent 的 ACK 响应示例：

```json
{
  "message": "chatResponseAck",
  "body": "{\"code\":0,\"message\":\"acknowledged\",\"deliveryId\":\"delivery_xxx\"}"
}
```

Agent 在最终 `chatResponse` 的 WebSocket `send_text()` 返回后记录本轮发送耗时；
该时刻只代表响应已交给连接，不代表媒体应用已处理。协商 `chatResponseAck` 时，
ACK 耗时通过独立事件记录，`ACK pending` 表示仍缺媒体侧应用确认。

`stream=true` 的中间响应 `agent -> media`（外层 `body` 仍是 JSON 字符串）：

```json
{"message":"chatResponse","body":"{\"message\":{\"chatIndex\":\"chat-1\",\"content\":{\"intentResult\":{\"description\":\"你\",\"status\":\"PROCESSING\"}}},\"sequence\":1,\"final\":false,\"display_only\":false,\"displayOnly\":false}"}
```

协商 `chatResponseAck` 后的成功终包 body（未协商时省略 `deliveryId`）：

```json
{
  "message": {
    "chatIndex": "对话索引",
    "content": {
      "intentResult": {
        "description": "",
        "status": "SUCCESS"
      }
    }
  },
  "display_only": true,
  "displayOnly": true,
  "sequence": 2,
  "final": true,
  "deliveryId": "delivery_xxx"
}
```

生成图片成功时，终包在 `intentResult` 中增加 `detail`。以下 Base64 仅为占位示例：

```json
{
  "chatIndex": "对话索引",
  "number": "13800138000",
  "messageType": "ANSWER",
  "display_only": false,
  "message": {
    "type": "BRIEF",
    "chatIndex": "对话索引",
    "content": {
      "intentExecution": {
        "description": "",
        "plans": [],
        "messageType": "ANSWER"
      },
      "intentResult": {
        "description": "图片已生成",
        "status": "SUCCESS",
        "plan": [],
        "messageType": "ANSWER",
        "detail": [
          {
            "type": "IMAGE",
            "imageId": "generated-image",
            "image": "<纯Base64图片数据>"
          }
        ]
      },
      "intentWeb": {
        "description": "",
        "resourceType": "",
        "resourceUrl": ""
      }
    }
  }
}
```

终包外层示例；前面已经发完正文时，终包 `description` 为空字符串：

```json
{
  "message": "chatResponse",
  "body": "{\"message\":{\"chatIndex\":\"chat-1\",\"content\":{\"intentResult\":{\"description\":\"\",\"status\":\"SUCCESS\"}}},\"display_only\":true,\"displayOnly\":true,\"sequence\":2,\"final\":true,\"deliveryId\":\"delivery_xxx\"}"
}
```

若已发送一个或多个中间包后运行失败，Agent 仍发送 `code=FAIL`、`final=true`
的失败终包以关闭本轮 stream；失败终包不重复已发送正文，也不携带
`deliveryId`，不能发送 `chatResponseAck`，对应 delivery 状态保持 `failed`。

### 4.3 audio

发送 `client -> agent`：

```json
{
  "userNumber": "用户号码",
  "audioIndex": "音频帧序号",
  "contents": [
    {
      "speakerNumber": "说话人号码",
      "audioContent": "音频数据Hex字符串",
      "time": "ISO时间戳"
    }
  ],
  "audioConfig": {
    "codec": "opus",
    "sampleRate": 16000,
    "channels": 1
  }
}
```

响应 `agent -> client`：

```json
{
  "code": 0,
  "message": "audio received"
}
```

当前 Agent 将 `audio` 作为传输层帧确认，不把原始音频内容送入 assistant prompt 或 provider。ASR/VAD/TTS 仍由媒体服务侧负责。

### 4.4 video

发送 `client -> agent`：

```json
{
  "userNumber": "用户号码",
  "videoIndex": "视频帧序号",
  "contents": [
    {
      "speakerNumber": "说话人号码",
      "videoContent": "视频数据Hex字符串",
      "time": "ISO时间戳"
    }
  ],
  "videoConfig": {
    "codec": "H264",
    "resolution": "1280x720",
    "frameRate": 30
  }
}
```

响应 `agent -> client`：

```json
{
  "code": 0,
  "message": "video received"
}
```

处理规则：

- 本次后台观察实现变化不修改 Media wire：`video` / `videoResponse` 的外层 envelope、`body` 字符串、字段名、H.264 Hex 编码和 ACK 语义保持不变。
- `videoContent` 必须是无 `0x` 前缀的 H.264 Annex-B Hex 字符串，并以三字节或四字节 NAL 起始码开头。
- 媒体服务必须让每条消息可独立解码：每帧包含 SPS、PPS 和 I-Frame，不依赖前后消息。
- Agent 使用本机 FFmpeg 把每条独立 H.264 消息解码为 JPEG；不再生成灰度指纹，也不运行像素差或 SSIM。
- 每个 session 复用统一 embedding coordinator。视频帧按固定 5 FPS 时间准入，每个准入帧只执行一次共享 SigLIP2 image embedding，并只根据 embedding cosine distance、首帧、交互提升和最长 10 秒间隔选帧。旧 2 FPS semantic probe 仅作为新输入 FPS 配置的迁移 alias，不再代表独立 probe。Qwen 视觉文本只在选帧之后生成，不反向参与本轮判定。收到 `chat` 时，冻结此前最新原始帧 sequence；需要时将该帧交互式提升并保护，工具只消费不晚于该边界的语义。
- 原始视频上下文只保留最近 3 帧；成功 VLM 结果发布到同 user/session 的 `SessionVisualSemanticStore`，同时保留 canonical text search embedding 和受限 evidence。它们不作为 Qwen 多帧历史发送。启用 `VisualContextService` 时，每轮 Qwen 请求只含当前选中的一张 JPEG，以及按独立 VLM tokenizer/limit 完成 token preflight 的旧 summary + 最近逐条文本；Provider 不再做 4,000 字符截断。未启用 visual compaction 时才回退到旧 rolling summary 的最多 2,000 字符兼容路径，并记录 `visual_context_compaction.status=unavailable`。semantic embedding 与 Qwen observation 各自最多 1 个执行中帧和 1 个 latest-wins pending，交互式目标不被替换，从而优先保证实时且无积压。
- 视觉上下文预算复用 `ContextWindowPolicy` 的 target/trigger/hard 语义，但使用独立 VLM 绝对预算和 reserve。压缩只覆盖最旧连续 prefix，成功并通过 coverage/revision 校验后才更新 summary；soft failure 保留旧 summary/raw records，hard failure 跳过本次后台 Provider，不阻塞已经完成的 `videoResponse` ACK，也会继续处理 latest pending 关键帧。
- 解码后的帧注册到当前 `AgentGraphRuntime.video_context_store`；原始 H.264 不落盘，也不进入 prompt、trace 或 Provider 请求。
- 选中关键帧的后台视觉理解经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> realtime_video_observe`；该内部 ToolSpec 不进入主 LLM catalog，WebSocket 入口也不直接调用 Provider。这里的成功分三层：`ToolResult.success is True` 只表示工具执行完成；内部 `VideoUnderstandingResult` 可验证且 `errors` 为空才算 VLM 语义成功；还必须 `source=background_keyframe_observation` 才允许发布为 rolling semantic snapshot。失败、partial、harness 说明性结果或 query-time `realtime_video_memory_unavailable` 结果只记录失败/可解释状态，不能写入 `current_state`。mock 模式不联网，真实连续 MLLM 只允许 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 配置。
- 同一连接后续 `chat` 会携带该 session 的 `video_id` 进入 Gateway。有 active video 时，AgentRuntime 可基于可信 entry profile 和结构化媒体引用动态暴露 `live_view_inspect`，不根据用户文本关键词暴露或调用工具。DeepSeek 只看到该工具的 schema；它看不到实时镜头可用状态、被动 `realtime_video_context`、视频帧、JPEG 路径、base64、VLM 角色模板、Qwen 原文或 provider raw response。
- 可信 Agent-Service 请求不会把 opaque `video_id`、镜头连接状态或后台语义快照渲染进主 LLM prompt。只有主 LLM 自主调用 `live_view_inspect` 后，工具 observation 才把当前实时镜头的有界视觉语义返回给它；最终回答不应向用户说“你刚发送的视频”、快照、后台观察或 Provider。
- 后台观察器持续把成功视觉文本发布到统一 `SessionVisualSemanticStore`；需要当前画面事实时，由 AgentRuntime 主 LLM 通过动态暴露的 `live_view_inspect` 表达需求。目标 sequence 已有记录时立即返回，仍在识别时最多等待 10 秒；等待期间提问之后到达的新帧不能替换该目标。达到等待上限后才退回到 `sequence <= 目标 sequence` 的最近成功记录；若此前仍无成功记录，才返回 unavailable。普通问候不应主动提及视觉。
- Revisioned visual summary 只供下一次后台 Qwen context projection；它不进入主 Agent prompt、conversation、Mem0 或 `visual_memory_search`。历史找物仍只从 raw `VisualSemanticRecord` 建立候选、as-of 过滤和排序，压缩失败或成功都不删除、改写这些记录。
- 每个连接始终最多一个 Qwen observation in-flight 和一个 latest-wins pending 帧；冻结的 chat 目标 sequence 在本轮期间受保护，不适用 pending latest-wins 替换。只有后台 observer 负责提交帧到 Qwen，不能绕开 observer 启动第二个 Qwen。
- 每个 `video_id` 只维护一个 persistent Qwen WebSocket；20 次成功观察或 60 秒后主动轮换，断线使用 0.25/0.5/1/2/5 秒封顶退避重连。失败保留最后成功快照并投影 `refreshing`/`stale`；切换 video id、连接关闭或 observer close 会关闭 Provider session 并清理 pending、快照、retained/raw JPEG 和临时文件。
- 新鲜度以成功语义对应帧的采集时间为主：`frame_capture_age_ms` 表示采集年龄，`snapshot_publish_age_ms` 表示 Qwen 结果发布年龄；`semantic_publish_latency_ms` 表示 Agent-Service 收到该视频消息到成功语义写入滚动记忆的端到端时延。采集时间缺失或在未来时不伪造年龄或时延。
- 普通上传/API（非 Agent-Service）调用 `media_inspect`：图片进入 image branch，明确上传的视频进入显式视频 Provider 路径；它不读取实时会话的滚动语义记忆。
- Agent-Service 携带视频引用的 chat turn 保留 90 秒 facade 总预算，用于覆盖 Gateway、AgentRuntime 主 LLM 执行、动态视觉工具调用以及可能较慢的上下文刷新。普通文本 chat 默认同样使用 90 秒，
  可通过 `ASSISTANT_AGENT_TEXT_TURN_TIMEOUT_SECONDS` 调整；主 Chat Provider 默认 75 秒，
  可通过 `MULTIMODAL_AGENT_CHAT_TIMEOUT_SECONDS` 调整，并应小于 facade 总预算。
- 连接建立 observer 时会持有 embedding coordinator 与视觉语义 store lease，idle TTL/LRU 不关闭活跃实例。连接关闭时先停止观察器、丢弃待处理帧并拒绝晚到结果；semantic embedding 或 Qwen 阻塞时分别受 `close_wait_seconds` 约束，不能无限卡住连接清理。随后释放 lease，并移除原始帧上下文和临时 JPEG。统一视觉语义存储不随 transport close 清理；它在 session/user clear、TTL eviction 或 runtime pool close 时连同 owned evidence 删除，使同 session 重连仍可查询。

成功响应中的 `video received` 表示该帧已经通过校验、成功解码、注册到视频上下文并完成本地选帧调度，不再只是传输层收到；它不表示后台视觉 MLLM 已完成。Hex、codec、NAL 起始码、大小或解码失败时返回 `videoResponse` 且 `body.code="FAIL"`；连接保持可用，失败帧不会附加到后续 chat。

#### H.264 编码和格式要求

媒体侧推荐从摄像头 I420/YUV 原始帧编码为独立 H.264 I-Frame，再把 H.264 bytes 转为小写 Hex 字符串放入 `videoContent`：

```text
摄像头 I420/YUV
  -> FFmpeg/libx264 H.264 Annex-B NALU
  -> videoData.toString("hex")
  -> video.body.contents[].videoContent
  -> Agent bytes.fromhex()
  -> H.264 解码为视频帧
  -> JPEG
  -> 本地视频上下文和全语义后台观察器
```

媒体侧 FFmpeg 编码参数应满足：

```text
-f rawvideo
-video_size {width}x{height}
-r {frameRate}
-pix_fmt yuv420p
-f h264
-preset ultrafast
-tune zerolatency
-b:v {bitrate}
-an
-frames 1
```

关键约束：

- 每条 `video` 消息必须是可独立解码的帧，不能依赖前后消息。
- 每帧必须包含 Annex-B 起始码，通常为 `00 00 00 01`。
- 每帧应包含 SPS、PPS 和 I-Frame，例如：

```text
00 00 00 01 67 ...  # SPS
00 00 00 01 68 ...  # PPS
00 00 00 01 65 ...  # IDR/I-Frame
```

媒体侧发送示例：

```javascript
const videoPackage = {
  userNumber: userNumber,
  videoIndex: videoIndexCounter.toString(),
  contents: [{
    speakerNumber: speakerNumber,
    videoContent: videoData.toString("hex"),
    time: timestampStr
  }],
  videoConfig: {
    codec: "H264",
    resolution: `${videoWidth}x${videoHeight}`,
    frameRate: actualVideoFrameRate,
    width: videoWidth,
    height: videoHeight
  }
};
```

格式链路对照：

| 阶段 | 格式 | 示例 |
| --- | --- | --- |
| 摄像头原始帧 | I420/YUV | `1280 * 720 * 1.5` bytes |
| 媒体编码输出 | H.264 Annex-B bytes | `00 00 00 01 67 ...` |
| WebSocket 传输 | Hex string | `"0000000167..."` |
| Agent 解码输入 | bytes | `b"\x00\x00\x00\x01\x67..."` |
| Agent 解码输出 | JPEG bytes | 单条消息解码得到的受控 JPEG |
| Agent 本地上下文 | `VideoFrame` 元数据 + JPEG 文件引用 | 原始 H.264 不落盘；不生成 grayscale fingerprint |

常见失败和排查：

| 现象 | 常见原因 | 检查方式 |
| --- | --- | --- |
| H.264 解码失败 | 缺少 Annex-B 起始码 | 检查 Hex 解码后是否包含 `00 00 00 01` |
| 解码后无帧 | Hex 截断或消息不完整 | 校验 `videoContent` 长度和 WebSocket 分包处理 |
| 花屏或无法独立解码 | 非 I-Frame 或缺少 SPS/PPS | 确认媒体侧每帧独立编码并带 SPS/PPS |
| `videoResponse` 为 `FAIL` | codec、大小、Hex 或解码校验失败 | 查看 `body.message`，连接可继续复用 |

调试时可在媒体侧临时落盘 H.264 bytes，并用本机工具验证：

```bash
ffplay -f h264 -i sample.h264
ffprobe -show_streams -select_streams v sample.h264
```

不要提交真实 `.h264`、JPEG、Base64 图片、Provider 请求或 Provider 原始响应。

三个视觉执行边界的结构化结果用 `source`、`media_kind` 和 `media_refs` 标明解析路径与证据来源：

| `source` | 含义 |
| --- | --- |
| `request_image` | `media_inspect` 解析明确图片引用 |
| `explicit_video` | `media_inspect` 解析明确上传的视频引用并调用 Provider |
| `rolling_video_memory` | `live_view_inspect` 读取受信 Agent-Service 会话的滚动语义记忆，不发生查询时视觉 Provider 调用 |
| `realtime_video_memory_unavailable` | Agent-Service 查询等待最多 10 秒后仍没有 A 之前可用的语义文本；目标仍在 observer 中时返回 `pending`，只有明确终态错误才返回 `failed`，并把可转述说明交给 LLM，不调用查询时视觉 Provider |
| `background_keyframe_observation` | 内部 `realtime_video_observe` 对选中关键帧执行的受治理后台分析 |

### 4.5 interrupt

发送 `client -> agent`：

```json
{
  "number": "用户号码"
}
```

响应 `agent -> client`：

```json
{
  "code": 0,
  "message": "interrupted"
}
```

`interrupt` 会先取消本连接当前活动和排队中的 `chat` turn，再返回成功 ACK。活动 turn 的取消经
`GatewayTurnFacade` 投递为 Gateway `run.cancel`，尚未进入 Gateway 的排队 turn 在入口层取消；
这里的“先取消”指 Gateway 已记录取消请求并立即关闭旧 turn 的可见输出门；AgentRuntime 在
provider/tool 前后的 cooperative cancellation checkpoint 实际停止执行。因此正在执行的 tool
可能完成，但晚到结果只能进入 trace 或显式允许复用的 artifact，不能恢复旧 turn 的播放。
已经被中断的旧 turn 不再发送后续
`chatResponse` delta 或终包。没有活动 turn 时该操作保持幂等成功，连接不会关闭，媒体可以继续
发送下一轮 `chat`。

### 4.6 生成媒体、渲染服务与 3D 服务接口

当前共有四段不同所有者的接口，不能把它们合并理解为 Agent 直连渲染服务：

| 编号 | 调用方 -> 接收方 | 协议 | 当前用途 | 本仓库所有权 |
| --- | --- | --- | --- | --- |
| A | Agent -> Media-Agent | `/agent-service/v1` WebSocket `chatResponse` | 交付文本、IMAGE、TD_MODEL、VIDEO | Agent 实现发送端 |
| B | Media-Agent -> Rendering Service | HTTP POST `/rendering/v1/torender` | 转发完整渲染消息 | 仓库外媒体服务约定 |
| C | `image_to_3d` -> 3D Service | HTTP POST `/3dgen/v1/openapi/img-to-3d` | 提交本地受管图片 | Agent 实现调用端 |
| D | 3D Service -> Agent | HTTP POST `/calling-agent-service/v1/{job_id}/{chat_index}/3d-gen-back` | 回传并保存模型、视频或图片；按任务投递策略可选转发媒体 | Agent 实现 callback 入口 |

新任务的 D 不再以媒体连接为完成前提：callback 先按独立 `job_id` 保存中性结果，只有任务提交时由
可信入口能力确定 `delivery_target=agent_service`，才继续通过 A 投递到活动媒体连接。HTTP Agent client
和通用 Gateway WebSocket 的默认 `delivery_target=none`，不会查询或写入媒体连接。

#### 4.6.1 Agent -> Media-Agent 与 Media-Agent -> Rendering Service

媒体服务是 `/agent-service/v1` 的 WebSocket client，Agent 是被动监听方。连接由媒体服务发送
`assistantControl` 建立。Agent 生成的图片以标准 `chatResponse` 复用这条连接发送给媒体服务：

```text
媒体服务 -- WebSocket /agent-service/v1 --> Agent
媒体服务 <-- chatResponse IMAGE -- Agent
```

这不表示媒体服务就是渲染服务。媒体服务内部将 `AGENT_SRC_RESPONSE` 链接到
`RenderingClient.RENDERING_SINK_DISPLAY`，再把完整 `chatResponse` JSON 通过 HTTP POST 转发到
渲染服务的 `/rendering/v1/torender`。Agent 不直连 `/torender`，也不建立独立的渲染 WebSocket。
媒体到渲染的 HTTP 转发属于媒体服务内部实现，Agent 不持有 `/torender` 连接。

媒体服务内部的数据流为：

```text
AgentClient
  ↓ AGENT_SRC_RESPONSE
RenderingClient.processDisplayInfo()
  ↓ HTTP POST application/json（完整 chatResponse JSON）
渲染服务 /rendering/v1/torender
```

外部媒体服务代码中的 `callLeg.js` 将 `AGENT_SRC_RESPONSE` 链接到
`RENDERING_SINK_DISPLAY`；`RenderingClient.js` 把原始 `chatResponse` JSON 转发到其部署配置的
`url1`、`url2`、`url3`。示例地址分别为 `8102`、`8202`、`8302` 端口下的
`/rendering/v1/torender`。这些地址不属于 Agent 配置。

按当前仓库记录的外部对接契约，HTTP 请求形态为：

```text
POST http://<rendering-host>:<rendering-port>/rendering/v1/torender
Content-Type: application/json
```

request body 是 Agent 发给 Media-Agent 的完整外层 `chatResponse` envelope，而不是单独的 Base64、
URL 或 `detail`：

```json
{
  "message": "chatResponse",
  "body": "{\"number\":\"13800138000\",\"message\":{\"type\":\"BRIEF\",\"chatIndex\":\"uuid\",\"content\":{\"intentResult\":{\"description\":\"\",\"status\":\"SUCCESS\",\"detail\":[{\"type\":\"IMAGE\",\"imageId\":\"generated-image\",\"image\":\"<纯Base64>\"}]}}}}"
}
```

Media-Agent 到渲染服务的 timeout、重试、鉴权、响应 schema 和多 URL 选择均不由本仓库实现；没有
外部 Media-Agent/Rendering Service 源码或联调证据时，本文不替它们声明额外保证。

其中 `chatResponse.body` 仍是 JSON 字符串；解码后的公共结构为：

```json
{
  "number": "13800138000",
  "message": {
    "type": "BRIEF",
    "chatIndex": "uuid",
    "content": {
      "intentResult": {
        "description": "渲染结果描述",
        "status": "SUCCESS",
        "detail": []
      }
    }
  }
}
```

Agent 当前发送的渲染 detail 为：

| 结果 | detail |
| --- | --- |
| 图片 | `{"type":"IMAGE","imageId":"<图片ID>","image":"<纯Base64>"}` |
| 3D 模型 | `{"type":"TD_MODEL","modelUrl":"<模型URL>"}` |
| 3D 视频 | `{"type":"VIDEO","videoUrl":"<视频URL>"}` |

##### 图片投递

`image_generation` Tool/runtime 只产出受管 artifact 与中性 `output_ref`，不直接持有或调用媒体
接口。只有 Agent-Service entry adapter 在构造该入口的最终响应时把 `output_ref` 解析为 `IMAGE`
detail，包含稳定 `imageId` 和纯 Base64 `image`，再发送给媒体 WebSocket。HTTP Agent client 和通用
Gateway WebSocket 不经过这段投影，因此单独调用生图不会向 Media-Agent 转发。Agent 不把图片发往
音视频媒体流接口，也不直接 POST `/torender`；由媒体服务的
`RenderingClient` 完成后续 HTTP 转发。该图片 `chatResponse.body` 必须明确携带
`display_only=false`；渲染服务实际依据
`message.content.intentResult.detail[].type="IMAGE"` 识别并处理图片，`display_only` 不替代
`detail[].type`。图片包使用媒体 legacy 完整结构，不携带纯文本流式协议的 camelCase display、
sequence、final 或 delivery ACK 扩展字段。

整体链路联调期间，生图插件内的开发常量 `DEVELOPMENT_IMAGE_FIXTURE_ID` 固定指向
`.local/generated/349cc6c272f4ec7a88800f0f.png`。启用该常量时，`image_generation` 保留模型
传入的 prompt，但直接返回该本地 artifact，不初始化或调用真实生图 Provider；主 LLM、媒体
WebSocket、媒体到渲染的转发以及后续 3D 服务调用仍按 real profile 工作。文件不存在、超限或内容
不是受支持图片时立即失败，不回退到付费 Provider。开发联调结束后将该常量设为 `None`，即可恢复
原有真实生图 Provider 路径；该临时开关不属于部署环境配置。

fixture 返回的 `imageId` 取文件名去掉扩展名。发给媒体的 `IMAGE.image` 仍在投递时从
`.local/generated` 读取；`image_to_3d` 也按同一 `imageId` 从该目录读取原文件，因此两条下游链路
使用的是同一份本地图片，不创建额外镜像。

#### 4.6.2 Agent -> 3D Service：图片转 3D 提交

`image_to_3d` 是模型可调用的受治理 Tool，必须经过
`ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。典型请求“生成3D蛋糕”先由 LLM 调用
`image_generation`；成功图片只保存为 Agent 的 `.local/generated` 受管 artifact，Tool observation
从本地文件名提取并返回 `{"image_id":["<图片ID>"]}`。LLM 随后调用 `image_to_3d` 时可以省略
`src_image`；运行时优先从同一 run 已成功的 `image_generation` 结果中绑定最近一个图片 ID。如果
用户在同一媒体 WebSocket 连接的后续 turn 说“继续生成3D”，则使用该连接上一 turn 最近成功生成的
图片 ID。显式提供 `src_image` 仍用于转换已有受管图片。Agent 与渲染服务
之间没有任何直连；`image_to_3d` 在同一 `.local/generated` 根目录内按 ID 安全解析 JPEG、PNG、
WebP 或 GIF 原文件，读取为纯 Base64 后调用 3D 服务：

```text
POST http://{TD_GEN_IP}:{TD_GEN_PORT}/3dgen/v1/openapi/img-to-3d
Content-Type: application/json
User-Agent: AgentService/1.0
```

```json
{
  "sessionId": "内部会话ID",
  "image": "图片Base64",
  "pre_cb_url": "http://{PUBLIC_IP}:{PUBLIC_PORT}/calling-agent-service/v1/{job_id}/0/3d-gen-back",
  "cb_url": "http://{PUBLIC_IP}:{PUBLIC_PORT}/calling-agent-service/v1/{job_id}/0/3d-gen-back",
  "format": "mp4"
}
```

提交字段：

| 字段 | 类型 | 当前来源与语义 |
| --- | --- | --- |
| `sessionId` | string | runtime-owned session ID；保留给 3D 服务关联请求，不再作为新 callback 的路径关联键 |
| `image` | string | 从 `.local/generated` 受管图片读取的纯 Base64，不带 Data URL 前缀 |
| `pre_cb_url` | string | 当前与 `cb_url` 完全相同 |
| `cb_url` | string | 3D 服务完成后回调的 Agent HTTP URL；路径携带独立随机 `job_id` |
| `format` | string | 当前固定为 `mp4`，不暴露给 LLM |

`src_image` 是模型可见的可选 string，只表示不带路径和后缀的图片 ID。Adapter 只在
`.local/generated` 下按 `.jpg`、`.jpeg`、`.png`、`.webp`、`.gif` 顺序解析文件，并重新验证
文件内容确实是受支持的图片。绝对路径、相对目录和目录穿越均不允许。

Adapter 在调用 3D 服务前创建进程内 `ImageTo3DJob`，记录 owner `user_id/session_id`、源图片 ID、
状态和 `delivery_target`。`image_to_3d` Tool 的结果包含该任务 ID：

```json
{"job_id":"image-to-3d-...","status":"generating","source_image_id":"<图片ID>"}
```

`delivery_target` 不是 LLM 或 HTTP caller 可写参数。Tool 只在 request metadata 同时满足可信
`agent_service_websocket + entry_profile=agent_service` 且 Gateway 声明
`supports_generated_media_delivery=true` 时设为 `agent_service`，否则固定为 `none`。HTTP `/agent/run`
会用自己的可信 capability 覆盖调用方伪造的同名 metadata。

为兼容当前 3D 服务契约，提交请求中的回调路径固定使用 `chat_index=0`。HTTP client 使用
`IMAGE_TO_3D_TIMEOUT_SECONDS` 作为单次 timeout，任何
HTTP 2xx 且响应体可被读取即视为提交成功；当前不解析、不校验 3D 服务响应 JSON，统一向 Tool 返回：

```json
{"job_id":"image-to-3d-...","status":"generating","source_image_id":"<图片ID>"}
```

超时、连接失败和非 2xx HTTP 响应统一映射为 `ImageTo3DError("无法生成，请检查网络~")`。

真实 Plugin 只有在 `provider_mode=real` 且 `TD_GEN_IP`、`TD_GEN_PORT`、`PUBLIC_IP`、
`PUBLIC_PORT` 全部存在时注册真实 `image_to_3d` Tool；mock mode 注册离线 adapter，不调用 3D 服务。

#### 4.6.3 3D Service -> Agent：完成回调

3D 服务完成后向 Agent 回传产物：

```text
POST /calling-agent-service/v1/{job_id}/{chat_index}/3d-gen-back
```

```json
{
  "mediaType": "ply",
  "mediaUrl": "http://10.243.227.110:8000/3dgen/v1/models/xxx.ply",
  "image": "可选Base64预览图"
}
```

callback 字段与当前校验如下：

| 字段 | 类型 | 必填 | 当前校验与用途 |
| --- | --- | --- | --- |
| path `job_id` | string | 是 | 新任务的独立随机 ID；先用于更新进程内 `ImageTo3DJobRegistry` |
| path `chat_index` | string | 是 | 路由必需但当前 handler 不使用；回调投递会生成新的 UUID |
| `mediaType` | string | 是 | Pydantic 必填；支持投影的值为 `ply`、`glb`、`mp4`、`image` |
| `mediaUrl` | string/null | 否 | `ply`、`glb`、`mp4` 投影时原样使用；当前不做 URL、scheme 或非空校验 |
| `image` | string/null | 否 | `mediaType=image` 时原样作为纯 Base64 使用；当前不解码、不保存、不校验大小 |

`mediaType` 映射如下：

| 3D 服务 `mediaType` | 媒体 detail |
| --- | --- |
| `ply` | `{"type":"TD_MODEL","modelUrl":"<mediaUrl>"}` |
| `glb` | `{"type":"TD_MODEL","modelUrl":"<mediaUrl>"}` |
| `mp4` | `{"type":"VIDEO","videoUrl":"<mediaUrl>"}` |
| `image` | `{"type":"IMAGE","image":"<Base64>"}` |

已登记任务的 callback 会先保存中性 `ImageTo3DArtifact(media_type, media_url, image)` 并把任务状态
更新为 `completed`。不支持的 `mediaType` 也会保存到已登记任务，但直接返回 HTTP 200
`{"code":"success"}`，不查询媒体连接、不发送 frame。缺少 `mediaType` 由 FastAPI/Pydantic 返回统一
HTTP 422 API error envelope。当前代码不会因
`mediaType=ply|glb|mp4` 缺少 `mediaUrl` 而返回 422，而会把 JSON `null` 投影给媒体；这是需要后续
收紧的现状，不应被客户端依赖。

HTTP client 使用 owner-bound 查询读取任务及结果：

```text
GET /agent/image-to-3d/jobs/{job_id}?user_id={user_id}&session_id={session_id}
```

查询经过与 `/agent/run` 相同的 API identity policy 和 trial access gate，并同时匹配任务记录中的
`user_id/session_id`；不存在或 owner 不匹配均返回 404。当前 registry 是单进程内存状态，不支持服务
重启恢复、多 worker 共享、过期清理或持久化；这些属于下一阶段 durable job store，而不是当前保证。

#### 4.6.4 Agent callback -> 活动 Media-Agent WebSocket

Agent 通过 runtime session ID 找到发起任务的活动媒体 WebSocket，并发送：

```json
{
  "message": "chatResponse",
  "body": "{\"number\":\"手机号码\",\"message\":{\"type\":\"BRIEF\",\"chatIndex\":\"新生成的uuid\",\"content\":{\"intentExecution\":{\"description\":\"\",\"plans\":[],\"messageType\":\"ANSWER\"},\"intentResult\":{\"description\":\"已为您生成3d模型，请查看\",\"status\":\"SUCCESS\",\"plan\":[],\"detail\":[{\"type\":\"TD_MODEL\",\"modelUrl\":\"http://TD_GEN_IP:PORT/models/xxx.ply\"}],\"messageType\":\"END\"},\"intentWeb\":{\"description\":\"\",\"resourceType\":\"\",\"resourceUrl\":\"\"}}}}"
}
```

回调 route 不重新进入 Gateway run。它先保存中性 3D job/artifact；仅当任务的
`delivery_target=agent_service` 时，才按任务记录的 runtime `session_id` 查找
`Rendering3DRelayRegistry` 中的活动媒体连接，并复用该连接既有的发送锁，避免与普通
`chatResponse` 并发写入。回调会按连接语言从对应
`mediaType` 的配置文案中随机选择一条描述，并为消息生成新的 UUID；`intentExecution.messageType`
保持 `ANSWER`，`intentResult.messageType` 设置为 `END`。WebSocket 发送成功后返回
`{"code":"success"}`；非媒体任务保存后直接返回同一成功响应，不要求媒体连接。需要媒体投递但没有
活动 session 或发送失败时异常向上转为 HTTP 500，任务的中性 completion 仍已保存。连接关闭时按
connection ID 注销绑定，旧连接的清理不能删除同 session 的新绑定。

迁移兼容：若 callback 路径第一段找不到已登记 `job_id`，route 仍把它当作旧 `session_id` 并沿用
直接媒体转发；新提交一律生成 `job_id` URL，不再产生这种旧格式。

Agent 不下载、保存、缓存或解析 `mediaUrl` 指向的产物。回调中继不创建新 Agent turn、不调用 LLM，
也不把产物 URL 写入 conversation history。

`src_image` 省略时先读取同一
run 最近一次成功 `image_generation` 的结构化 `image_id`，再读取当前媒体连接上一 turn 保存的
最近图片 ID；两者都不存在时明确要求先生成图片，不会猜测全局最新文件。连接关闭后该引用随连接
状态清除，不跨用户或跨进程恢复。产物 `format` 不暴露给 LLM，Agent 固定向 3D 服务提交 `mp4`。
Tool 不查询渲染服务，也不读取 `.local/generated` 之外的路径。发给媒体的 `IMAGE.image` 同样通过受管
`output_ref` 从该本地目录读取，不使用 Provider 临时 URL，也不写第二份镜像。
配置和错误语义如下：

| 参数 | 说明 | 示例 |
| --- | --- | --- |
| `TD_GEN_IP` / `TD_GEN_PORT` | 3D 生成服务地址 | `10.243.227.110:8000` |
| `PUBLIC_IP` / `PUBLIC_PORT` | 3D 服务可回调的 Agent 地址 | 部署可达地址 |
| `IMAGE_TO_3D_TIMEOUT_SECONDS` | HTTP 请求单次超时 | `5` |

上述地址由 `ImageTo3DToolPlugin` 在 real mode 组装；`TD_GEN_IP`、`PUBLIC_IP` 当前只接受 host/IP
配置，协议固定为 `http`，提交 path 和 callback path 由代码固定。任一必需地址配置缺失时，真实
`image_to_3d` Tool 不注册，不回退到 mock。

图片不存在返回 `图片不存在：<图片ID>`，HTTP 网络失败返回 `无法生成，请检查网络~`。日志不得记录
Base64、真实用户内容或服务原始响应。当前 adapter 不记录 3D 服务响应 body。

要证明一次真实渲染完整成功，必须分别确认 Agent WebSocket send、媒体 `RenderingClient` POST、
渲染服务接收/处理；Agent 日志中的 send 成功只能证明第一跳写入媒体连接。

## 5. 错误处理

| 场景 | 响应 |
| --- | --- |
| URL 版本不是 `/agent-service/v1` | 返回 `error`，随后 WebSocket close code `1008` |
| 外层不是 JSON object | 返回 `error` |
| 缺少外层 `message` | 返回 `error` |
| 外层 `body` 不是 JSON 字符串 | 返回当前消息对应响应类型，`body.code=\"FAIL\"` |
| `body` 字符串不是 JSON object | 返回当前消息对应响应类型，`body.code=\"FAIL\"` |
| 缺少必填字段 | 返回当前消息对应响应类型，`body.code=\"FAIL\"` |
| `videoContent` 非法、超过大小限制或无法解码 | 返回 `videoResponse`，`body.code=\"FAIL\"`；连接保持可用 |
| 未知 `message` 类型 | 返回 `error` |
| Gateway 超时或后端错误 | 返回 `chatResponse`，`body.code=\"FAIL\"`；本地 `run_client.py` 会在 stderr 显示失败原因并以非零状态结束。若 runtime 已启动，失败 delivery audit 与 trace terminal summary 保留统一的 `run_id` 与独立的 `trace_id`；超时时 runtime 状态先记为 `pending_cancel`，以后续真实取消/失败事件为准。 |
| 3D 回调缺少 `mediaType` | FastAPI/Pydantic HTTP 422 API error envelope；不转发 |
| 3D 回调 `mediaType` 不支持 | HTTP 200 `{"code":"success"}`；不查 relay、不转发 |
| 3D 回调缺少对应 `mediaUrl` / `image` | 当前仍尝试转发 JSON `null`；不会因该字段缺失返回 422 |
| 3D 回调没有活动媒体 session 或 WebSocket send 失败 | 未处理异常转为 HTTP 500；不返回成功 ACK |
| WebSocket 异常断开 | 记录 ERROR 级安全日志；取消当前 session 的活动 Gateway run |

通用 `error` 示例：

```json
{
  "message": "error",
  "body": "{\"code\":\"FAIL\",\"message\":\"unknown message type: notSupported\"}"
}
```

## 6. 完整通信流程

```text
App                 Media                         Agent
 |                    |                              |
 |== media/session ==>+== WebSocket connect =======>|
 |                    |== assistantControl ========>|
 |                    |<== assistantControl ========|
 |== ASR/chat =======>|== chat(stream=true) =======>|
 |<== delta/TTS ======|<== chatResponse PROCESSING==|
 |<== final/TTS ======|<== chatResponse SUCCESS ====|
 |== audio/video ====>|== audio/video =============>|
 |                    |<== audio/videoResponse ======|
 |== interrupt ======>|== interrupt ================>|
 |                    |<== interrupt ================|
 |== close ==========>|== close ====================>|
```

## 7. 本地验证

启动 Agent：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --provider mock \
  --image-provider mock \
  --host 0.0.0.0 \
  --port 8089
```

真实 Qwen 视频理解必须使用进程级显式选择；仅在 `.env` 中存在 Key 不会自动启用真实 Provider：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
MULTIMODAL_AGENT_VISION_PROVIDER=qwen \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --host 0.0.0.0 \
  --port 8089
```

Qwen/DashScope API key 统一使用 `QWEN_API_KEY`（兼容 `DASHSCOPE_API_KEY`；旧的
`QWEN_VISION_API_KEY` 仍作为迁移期别名）。
推荐配置百炼业务空间专属 WebSocket endpoint：设置 `QWEN_REALTIME_VISION_WORKSPACE_ID`
和 `QWEN_REALTIME_VISION_REGION`（默认 `cn-beijing`；可选 `ap-southeast-1`）后，运行时会生成
`wss://{WorkspaceId}.{region}.maas.aliyuncs.com/api-ws/v1/realtime`。显式
`QWEN_REALTIME_VISION_BASE_URL` 优先级最高，可用于覆盖 workspace endpoint；未配置 workspace
时仍保留旧 `wss://dashscope.aliyuncs.com/api-ws/v1/realtime` 默认以兼容现有环境。普通图片仍使用
`QWEN_VISION_BASE_URL` / `QWEN_VISION_MODEL` 对应的图片 HTTP adapter；视频理解不再有独立
`video_provider` selector，实时视频和视频工具都以 `MULTIMODAL_AGENT_VISION_PROVIDER` 为唯一
provider 选择入口。启动前应确认 chat readiness 和视觉插件的 vision provider
配置；显式选择 Qwen 失败时不会静默回退到 Ark、Doubao 或 mock。

本地统一 embedding 只有在 real mode 下显式设置
`MULTIMODAL_AGENT_EMBEDDING_PROVIDER=local_siglip2` 与
`SIGLIP2_MODEL_DIR` 时启用。旧 vision-prefixed 名称是迁移 alias。该目录必须由 operator 预先导出，Runtime 不联网下载；可选
`SIGLIP2_CUDA_DEVICE_ID` 默认是 `0`。选帧参数可通过
`REALTIME_KEYFRAME_MAX_INTERVAL_SECONDS`（默认 `10`）、
`REALTIME_SEMANTIC_INPUT_FPS`（默认 `5`）、
`REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS`（默认 `0.5`）和
`REALTIME_KEYFRAME_SEMANTIC_THRESHOLD`（默认 `0.18`）调整。旧
`REALTIME_KEYFRAME_SEMANTIC_PROBE_FPS` 是输入 FPS 的迁移 alias；structural/combined 配置已拒绝。
schema v2 ONNX 资产同时包含 image
`visual_projection`、text projection 和 tokenizer，并由同一 revision/space 约束；schema v1 image-only
资产只能报告 `text_ready=false`。模型资产、依赖、checksum 或 CUDA
不可用时不会回退到像素或 SSIM；只有交互 pin 或最长间隔可继续触发降级 VLM observation。
视觉上下文压缩由 `REALTIME_VISUAL_CONTEXT_COMPACTOR` 显式启用；real LLM compactor 必须配置本地
`REALTIME_VISUAL_CONTEXT_TOKENIZER_PATH`，不会联网下载 tokenizer。视觉 input limit、
target/trigger/hard ratio、safety margin、summary max tokens、最近 record 数和 instruction/image/output
reserve 均由对应 `REALTIME_VISUAL_CONTEXT_*` 配置独立控制。
短期视觉时间线、历史找物、session cleanup 和 as-of 规则以
`docs/multimodal-embedding-architecture.md` 为准。

真实联调只记录脱敏证据：provider mode、chat/video provider、Qwen model、
后台观察状态和 latency、工具结果的 `source`、用户可见最终回复、
`chatResponse` 状态以及 WebSocket close code。不得记录 Key、Base64 图片、
绝对路径、Provider 请求体或原始响应。`videoResponse body.code=0` 只证明帧已成功
校验、解码、注册和调度；后台视觉理解成功还必须由 provider/model/status 证据确认。

真实联调通过 `scripts/run_server.py` 与 `scripts/run_client.py` 的显式 operator 流程执行，不放入
pytest。只有同时选择 real provider mode、显式配置 Qwen vision provider 和
本机未跟踪凭据时才允许联网；具体入口和参数见 `scripts/README.md`。

当前性能目标是 first delta `<500ms`、total observation `<1s`。smoke 始终输出实际值和
是否达到目标；目标未达到时不得伪造通过数据，也不把环境延迟与协议正确性混为一谈。
默认 pytest 不加载真实 `.env` 或调用网络。

### 7.1 单轮耗时诊断

`scripts/run_server.py` 默认启用非阻塞 trace 持久化。收到安全 INFO 日志中的
`trace` 后，默认直接从本地机器级 JSONL trace 生成视图；入口与 Assistant Runtime
共享同一个 `run_id`，LLM/工具事件使用该 `run_id` 与独立 `trace_id` 关联：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py trace_xxx
```

输出中的 `bottleneck` 是本轮最大关键路径阶段。常见慢点映射如下：

| 诊断项 | 含义 |
| --- | --- |
| `chat_queue_wait` | 同一 session 的上一轮仍在执行，本轮等待串行锁。 |
| `conversation_prepare` | 会话历史读取、上下文请求准备较慢。 |
| `llm_chat[1]` | 首次 LLM 工具选择/直接回答调用较慢。 |
| `tool_execute[media_inspect]` | 普通上传/API 对明确图片或视频执行视觉理解；显式视频的查询时 Provider 工作计入这里。 |
| `tool_execute[live_view_inspect]` | AgentRuntime 动态暴露后，由主 LLM 读取/检查滚动语义文本；不执行查询时帧识别。 |
| `tool_execute[realtime_video_observe]` | Agent-Service 后台 observer 对选中关键帧执行 Qwen observation。 |
| `llm_chat[2]` | 工具观察后的最终回答 LLM 调用较慢。 |
| `websocket_send` | socket/媒体接收端产生传输背压。 |
| `ACK pending` | 最终响应已发送，但媒体应用确认尚未到达。 |
| `frame_capture_age_ms` 较高 | 本轮消费的语义对应 Media 帧采集时间较早，是画面陈旧度主指标。 |
| `snapshot_publish_age_ms` 较高 | Qwen 结果发布后已过去较长时间。 |
| `sequence_gap` 大于 0 | 10 秒目标序号等待仍未取得精确目标；回答只能使用目标 sequence 之前的最近成功文本，并保留画面可能滞后的不确定性。 |
| `semantic_publish_latency_ms` 较高 | 从 WebSocket 收到目标视频消息到成功语义发布较慢；该值包含解码、选帧、排队和视觉 observation。 |
| `unattributed` | 端到端耗时中尚未被叶子阶段解释的剩余部分。 |

视频诊断中的后台观察 latency 不直接计入 chat 关键路径。普通上传/API 的显式视频
Provider 调用会体现在 `tool_execute[media_inspect]`；Agent-Service 动态视觉工具调用只
读取/报告滚动语义文本状态，不执行查询时帧识别。画面陈旧度主要通过
`frame_capture_age_ms`、`snapshot_publish_age_ms` 和 `sequence_gap` 体现。
`videoResponse(code=0)` 仍仅是帧校验、
解码、注册与调度成功的证据，不是 MLLM 完成证据。

默认日志、trace、`.data/graph_trace.jsonl` 和 delivery audit 均不含对话正文。
确需确认分析的是哪一轮时，只能在本机调试进程显式开启正文查询：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --provider mock --image-provider mock --host 127.0.0.1 --port 8089 \
  --allow-local-trace-content
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py trace_xxx \
  --server http://127.0.0.1:8089 --include-conversation
```

正文查询默认关闭，只接受 loopback 客户端，每侧最多返回 1000 个 Unicode 字符，
且只返回当前 trace 对应的用户文本和最终回复。不得在共享或生产进程启用，也不得
把终端正文重新写入日志、trace 或联调证据。

使用任意 WebSocket 客户端连接：

```text
ws://127.0.0.1:8089/agent-service/v1
```

Python 示例：

```python
import asyncio
import json

import websockets


def envelope(message: str, body: dict) -> str:
    return json.dumps(
        {"message": message, "body": json.dumps(body, ensure_ascii=False)},
        ensure_ascii=False,
    )


async def main() -> None:
    async with websockets.connect("ws://127.0.0.1:8089/agent-service/v1") as websocket:
        await websocket.send(
            envelope(
                "assistantControl",
                {"number": "10086", "callType": "AUDIO", "modelName": "mock"},
            )
        )
        print(await websocket.recv())

        await websocket.send(
            envelope(
                "chat",
                {
                    "chatIndex": "chat-1",
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "你好",
                            "time": "2026-07-09T10:00:00+08:00",
                        }
                    ],
                    "stream": True,
                },
            )
        )
        print(await websocket.recv())


if __name__ == "__main__":
    asyncio.run(main())
```

## 8. 实现边界

- `/agent-service/v1` 是唯一 Media Service WebSocket 入口，不是新的 Agent 主循环。
- `assistantControl` 建立媒体连接上下文，不绕过 provider/runtime policy。
- `chat` 进入 Gateway 和 assistant runtime；`audio` 返回传输层 ACK；`interrupt` 取消活动 Gateway
  turn 和尚未进入 Gateway 的排队 chat turn 后返回 ACK。
- `video` 在入口层完成严格校验和 H.264 I-Frame 到 JPEG 的受控解码，后续 `chat` 只把稳定 `video_id` 送入 Gateway；入口层不直接调用视频 Provider。
- 默认 mock/local/offline 运行不会调用真实外部 Provider；真实 Provider 只在显式 profile 和本机安全配置允许时启用。
- `image_to_3d` 是受治理生成 Tool；只消费 Agent 受管图片 artifact。省略 `src_image` 时可使用同一
  run 最近的生图结果，Agent-Service 入口还兼容本连接上一 turn 的最近图片。真实 3D POST 只在
  real 模式且配置完整时执行。
- 3D callback route 当前只强制 `mediaType`，按 `mediaType` 映射并向活动媒体 WebSocket 转发
  `mediaUrl` 或 `image`；它不下载、存储或解析 3D 产物，也不进入 Agent 规划。该 route 当前尚未
  与媒体投递解耦，不能作为非媒体入口的完成结果存储。
- 不要在该接口中传输 API key、token、provider 原始响应或未脱敏敏感数据；原始音视频大 payload 不进入 prompt。
