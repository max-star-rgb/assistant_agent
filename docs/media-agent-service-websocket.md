# Media-Agent WebSocket 接口权威文档

Last updated: 2026-07-16

本文档是媒体服务与 `assistant_agent` 之间 `/agent-service/v1` WebSocket 传输层协议的唯一权威文档，合并了旧临时 Mock Agent 协议说明和旧 H.264 视频传输专项说明。媒体侧协议为外部对接基准；Agent 侧负责兼容该协议，并在内部把 `chat` 文本请求转入 Gateway 和 assistant runtime。

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

- 如果外层带 `sessionId`，Agent 会把它作为内部 Gateway session id，并在响应中回传。
- 如果外层不带 `sessionId`，Agent 会从 `assistantControl.body.number`、`chat.body.userNumber`、`audio.body.userNumber`、`video.body.userNumber` 或 `interrupt.body.number` 派生内部 session id，响应默认不额外增加外层 `sessionId`。
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

### 3.2 Agent -> 客户端

| message 类型 | 说明 | body 结构 |
| --- | --- | --- |
| `assistantControl` | 连接响应 | 参见 4.1 |
| `chatResponse` | 文本响应 | 参见 4.2 |
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
  "clientCapabilities": {"chatProgress": true, "chatResponseAck": true}
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `number` | string | 是 | 用户号码；无外层 `sessionId` 时也作为内部 session id |
| `callType` | string | 是 | `AUDIO` 或 `VIDEO` |
| `modelName` | string | 否 | 媒体侧希望使用的模型名称；Agent 当前记录但不直接用它绕过 provider/runtime policy |
| `clientCapabilities.chatProgress` | boolean | 否 | 为 true 时立即并每 15 秒发送 `chatProgress` |
| `clientCapabilities.chatResponseAck` | boolean | 否 | 为 true 时最终响应携带 `deliveryId`，媒体处理后回 ACK |

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
| `userNumber` | string | 是 | 用户号码；无外层 `sessionId` 时也作为内部 session id |
| `contents` | array | 是 | 至少一条内容 |
| `contents[].speakerNumber` | string | 是 | 说话人号码 |
| `contents[].speechContent` | string | 文本消息必填 | 已完成 ASR 的文本 |
| `contents[].imageContent` | string | 图片消息可选 | 图片 Base64；当前作为媒体兼容字段接收，不直接进入图像 provider |
| `contents[].time` | string | 是 | ISO 8601 时间戳 |
| `stream` | boolean | 否 | 为 `true` 时，真实 Provider token delta 投影为多个 `chatResponse` 中间包，之后发送一个成功终包关闭本轮 stream；缺省或 `false` 时只发送终包 |

处理规则：

- Agent 使用最新一条非空 `speechContent` 作为本轮 Gateway 输入文本。
- 只包含 `imageContent` 的内容项可以随请求传入，但当前不单独触发图像理解。
- `chat` 会进入 `GatewayTurnFacade -> GatewaySessionManager -> GatewayAgentAdapter -> AssistantRuntimeApp -> AgentGraphRuntime`。
- chat run 在独立任务中执行，WebSocket 主循环会继续接收并 ACK 后续媒体消息。
- `stream=true` 的中间包只携带本包新增文本，`status=PROCESSING`、`sequence>=1`、`final=false`；终包只携带尚未发送过的剩余文本，`status=SUCCESS`、最后一个 `sequence`、`final=true`。
- 若本轮正文已经全部通过中间包发送，成功终包的 `description` 为空字符串。媒体/App 可以继续按增量追加处理，不会重复追加完整答案。
- 若本轮已经发送过中间包，成功终包仍会同时携带 `display_only=true` 和 camelCase 兼容字段 `displayOnly=true`，供支持该标记的客户端识别终包。
- 只有真实 Provider token delta 产生中间包；Provider 不支持或未产生 token delta 时，即使 `stream=true` 也只发送一个完整终包，不伪造流式能力。
- 购物推荐/比价成功时，Agent 侧由 deterministic presenter 根据 `shopping_search` / `price_compare` 结构化结果生成 App 购物协议；成功终包的 `description` 可以包含自然语言摘要和唯一 `<detail>...</detail>` 块，媒体侧按文本透传，App 侧按协议渲染。LLM 不应手写 `<detail>` 字段。
- `deliveryId` 和 `chatResponseAck` 只属于成功终包；中间包和失败终包都不进入应用层 ACK 状态。
- Provider 的工具调用前导文本受 runtime commit barrier 保护；会被工具调用取代的 provisional 文本不会发送给 Media/App。

协商 `chatProgress` 后，Agent 立即并每 15 秒发送一次：

```json
{"message":"chatProgress","body":"{\"chatIndex\":\"chat-1\",\"deliveryId\":\"delivery_xxx\",\"status\":\"PROCESSING\"}"}
```

协商 `chatResponseAck` 后，最终响应增加 `deliveryId`。媒体处理完成后发送：

```json
{"message":"chatResponseAck","body":"{\"deliveryId\":\"delivery_xxx\",\"chatIndex\":\"chat-1\"}"}
```

Agent 返回 `chatResponseAck` 且 `code=0` 才表示应用层 ACK 已记录。媒体端对视频理解 turn 的等待时间必须至少为 90 秒。

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
- Agent 使用本机 FFmpeg 在一次解码中同时生成 JPEG 和 `32x18` 灰度指纹；指纹只用于本地变化检测，不进入 prompt、trace 或 Provider 请求。
- 每个连接维护一个本地自适应观察器：首帧必选，明显变化立即成为候选，静态画面最长 2 秒产生一次候选；再把选中帧交给后台串行任务，不退化为固定 2 秒轮询。
- 原始视频上下文只保留最近 3 帧；成功理解的语义关键帧独立保留最多 8 帧。它们是本地 fallback/语义记忆，不作为 Qwen 多帧历史发送。每轮 Qwen 请求只含当前选中的一张 JPEG 和最多 2,000 字符的上一成功语义摘要。后台队列最多包含 1 个执行中帧和 1 个待处理帧，积压时用最新候选替换旧候选。
- 解码后的帧注册到当前 `AgentGraphRuntime.video_context_store`；原始 H.264 不落盘，也不进入 prompt、trace 或 Provider 请求。
- 选中关键帧的后台视觉理解复用 `video_understanding`，并经过 `ActionValidator -> ToolExecutor -> ToolRegistry`；WebSocket 入口不直接调用 Provider。这里的成功分三层：`ToolResult.success is True` 只表示工具执行完成；`VideoUnderstandingResult` 可验证且 `errors` 为空才算 VLM 语义成功；还必须 `source=background_keyframe_observation` 才允许发布为 rolling semantic snapshot。失败、partial、harness 说明性结果或 query-time `realtime_video_memory_unavailable` 结果只记录失败/可解释状态，不能写入 `current_state`。默认 `local_demo` / `offline_eval` 不联网，真实连续 MLLM 只允许显式 `provider_smoke` / `pilot` 配置。
- 同一连接后续 `chat` 会携带该 session 的 `video_id` 进入 Gateway。有 active video 时，AgentRuntime 可基于可信 entry profile 和结构化媒体引用动态暴露 `video_understanding`，不根据用户文本关键词暴露或调用工具。DeepSeek 只知道可以调用该视觉理解 tool，并看到实时镜头可用和被动 `realtime_video_context` 文本 snapshot；它看不到视频帧、JPEG 路径、base64、VLM 角色模板、Qwen 原文或 provider raw response。
- 可信 Agent-Service 请求把它表述为双方共享的当前实时镜头，不把 opaque `video_id` 渲染成上传视频，也不应向用户说“你刚发送的视频”、快照、后台观察或 Provider。
- 后台观察器仍作为预热缓存运行；需要当前画面事实时，由 AgentRuntime 主 LLM 通过动态暴露的 `video_understanding` 表达需求。Agent-Service 的查询时工具调用只读取后台已经产出的滚动语义文本；若文本尚未就绪或最新观察失败，工具返回一段可直接转述给用户的说明，并附带 `pending` / `failed` / `unavailable` 状态，不临时发送原始帧给视觉 Provider。普通问候不应主动提及视觉。
- 每个连接始终最多一个 Qwen observation in-flight 和一个 latest-wins pending 帧；只有后台 observer 负责提交帧到 Qwen，不能绕开 observer 启动第二个 Qwen。
- 每个 `video_id` 只维护一个 persistent Qwen WebSocket；20 次成功观察或 60 秒后主动轮换，断线使用 0.25/0.5/1/2/5 秒封顶退避重连。失败保留最后成功快照并投影 `refreshing`/`stale`；切换 video id、连接关闭或 observer close 会关闭 Provider session 并清理 pending、快照、retained/raw JPEG 和临时文件。
- 新鲜度以成功语义对应帧的采集时间为主：`frame_capture_age_ms` 表示采集年龄，`snapshot_publish_age_ms` 表示 Qwen 结果发布年龄；采集时间缺失或在未来时不伪造采集年龄。
- 普通上传/API（非 Agent-Service）仍可显式调用 `video_understanding`：最新观察成功时读取滚动语义记忆，记忆未就绪或最新观察失败时使用最近 3 帧走 Provider 回退。
- Agent-Service 携带视频引用的 chat turn 保留 90 秒 facade 总预算，用于覆盖 Gateway、AgentRuntime 主 LLM 执行、动态视觉工具调用以及可能较慢的上下文刷新；普通 chat 使用 30 秒。
- 连接关闭时先停止观察器、丢弃待处理帧并拒绝晚到结果，再移除滚动语义记忆、关键帧、原始帧上下文和 JPEG 运行时文件。

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
  -> JPEG + 灰度指纹
  -> 本地视频上下文和后台观察器
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
| Agent 解码输出 | video frame | `av.VideoFrame` 或等价结构 |
| Agent 本地上下文 | JPEG + grayscale fingerprint | Prompt-safe 引用和统计 |

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

受治理后台观察与普通上传/API `video_understanding` 的结构化结果用 `source` 标明解析路径：

| `source` | 含义 |
| --- | --- |
| `rolling_video_memory` | Agent-Service 或普通上传/API 查询读取滚动语义记忆，未发生查询时视觉 Provider 调用 |
| `realtime_video_memory_unavailable` | Agent-Service 查询时没有可用语义文本；工具把可转述说明和待就绪/失败/不可用状态返回给 LLM，不调用视觉 Provider |
| `recent_frame_fallback` | 普通上传/API 在记忆未就绪或最新观察失败时使用最近原始帧调用 Provider |
| `background_keyframe_observation` | 持续观察器对选中关键帧执行的受治理后台分析 |

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

当前 `/agent-service/v1` 的 `interrupt` 是媒体兼容 ACK。Gateway 原生取消/打断语义仍由 normalized Gateway 或 `/ws/realtime/media` 路径承载。

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
| Gateway 超时或后端错误 | 返回 `chatResponse`，`body.code=\"FAIL\"` |

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
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke \
MULTIMODAL_AGENT_VISION_PROVIDER=qwen \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --host 0.0.0.0 \
  --port 8089
```

Qwen realtime 视频适配器优先使用 `QWEN_VISION_API_KEY`（缺省回退到 `DASHSCOPE_API_KEY`），
并读取 `QWEN_REALTIME_VISION_BASE_URL` 和 `QWEN_REALTIME_VISION_MODEL`。普通图片仍使用
`QWEN_VISION_BASE_URL` / `QWEN_VISION_MODEL` 对应的图片 HTTP adapter；视频理解不再有独立
`video_provider` selector，实时视频和视频工具都以 `MULTIMODAL_AGENT_VISION_PROVIDER` 为唯一
provider 选择入口。启动前应确认 chat readiness 和 `video_understanding` 的 vision provider
配置；显式选择 Qwen 失败时不会静默回退到 Ark、Doubao 或 mock。

真实联调只记录脱敏证据：runtime profile、chat/video provider、Qwen model、
后台观察状态和 latency、工具结果的 `source`、用户可见最终回复、
`chatResponse` 状态以及 WebSocket close code。不得记录 Key、Base64 图片、
绝对路径、Provider 请求体或原始响应。`videoResponse body.code=0` 只证明帧已成功
校验、解码、注册和调度；后台视觉理解成功还必须由 provider/model/status 证据确认。

仓库的 opt-in smoke 覆盖单帧、连续五帧后最终完成 sequence、主动断线后的新 session
恢复，以及首 delta/完整观察耗时。只有同时设置 `RUN_INTEGRATION_TESTS=1`、
`MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke|pilot`、
`MULTIMODAL_AGENT_VISION_PROVIDER=qwen` 和 `QWEN_VISION_API_KEY`（或 fallback
`DASHSCOPE_API_KEY`）时才会联网：

```bash
RUN_INTEGRATION_TESTS=1 \
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke \
MULTIMODAL_AGENT_VISION_PROVIDER=qwen \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/integration/test_qwen_realtime_vision_provider_smoke.py -q -s
```

当前性能目标是 first delta `<500ms`、total observation `<1s`。smoke 始终输出实际值和
是否达到目标；目标未达到时不得伪造通过数据，也不把环境延迟与协议正确性混为一谈。
未满足任一 opt-in 条件时用例必须 skip，默认测试不得加载真实 `.env` 或调用网络。

### 7.1 单轮耗时诊断

`scripts/run_server.py` 默认启用非阻塞 trace 持久化。收到安全 INFO 日志中的
`trace` 后，默认直接从本地机器级 JSONL trace 生成视图；`gateway_run` 是 Gateway
包装 run，`assistant_run` 才是承载 LLM/工具事件的 Assistant run：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py trace_xxx
```

输出中的 `bottleneck` 是本轮最大关键路径阶段。常见慢点映射如下：

| 诊断项 | 含义 |
| --- | --- |
| `chat_queue_wait` | 同一 session 的上一轮仍在执行，本轮等待串行锁。 |
| `conversation_prepare` | 会话历史读取、上下文请求准备较慢。 |
| `llm_chat[1]` | 首次 LLM 工具选择/直接回答调用较慢。 |
| `tool_execute[video_understanding]` | 普通上传/API 显式调用视觉工具，Agent-Service 后台 observer 执行 Qwen observation，或 AgentRuntime 动态暴露后由主 LLM 读取/检查滚动语义文本。 |
| `llm_chat[2]` | 工具观察后的最终回答 LLM 调用较慢。 |
| `websocket_send` | socket/媒体接收端产生传输背压。 |
| `ACK pending` | 最终响应已发送，但媒体应用确认尚未到达。 |
| `frame_capture_age_ms` 较高 | 本轮消费的语义对应 Media 帧采集时间较早，是画面陈旧度主指标。 |
| `snapshot_publish_age_ms` 较高 | Qwen 结果发布后已过去较长时间。 |
| `sequence_gap` 大于 0 | 1.5 秒目标序号屏障未满足；回答必须保留画面可能滞后的不确定性。 |
| `unattributed` | 端到端耗时中尚未被叶子阶段解释的剩余部分。 |

视频诊断中的后台观察 latency 不直接计入 chat 关键路径。普通上传/API 的
`recent_frame_fallback` 会体现在 `tool_execute[video_understanding]`；Agent-Service
动态视觉工具调用只读取/报告滚动语义文本状态，不执行查询时帧识别。画面陈旧度主要通过
`frame_capture_age_ms`、`snapshot_publish_age_ms` 和 `sequence_gap` 体现。
`videoResponse(code=0)` 仍仅是帧校验、
解码、注册与调度成功的证据，不是 MLLM 完成证据。

默认日志、trace、`.data/graph_trace.jsonl` 和 delivery audit 均不含对话正文。
确需确认分析的是哪一轮时，只能在本机调试进程显式开启正文查询：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --provider mock --image-provider mock --host 127.0.0.1 --port 8089 \
  --allow-local-trace-content
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py trace_xxx \
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

- `/agent-service/v1` 是媒体服务兼容入口，不是新的 Agent 主循环。
- `assistantControl` 建立媒体连接上下文，不绕过 provider/runtime policy。
- `chat` 进入 Gateway 和 assistant runtime；`audio`、`interrupt` 当前返回传输层 ACK。
- `video` 在入口层完成严格校验和 H.264 I-Frame 到 JPEG 的受控解码，后续 `chat` 只把稳定 `video_id` 送入 Gateway；入口层不直接调用视频 Provider。
- 默认 mock/local/offline 运行不会调用真实外部 Provider；真实 Provider 只在显式 profile 和本机安全配置允许时启用。
- 不要在该接口中传输 API key、token、provider 原始响应或未脱敏敏感数据；原始音视频大 payload 不进入 prompt。
