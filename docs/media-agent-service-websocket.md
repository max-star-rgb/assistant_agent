# Media 到 Agent WebSocket 接口文档

Last updated: 2026-07-09

本文档描述真实媒体服务与 `assistant_agent` 之间的 WebSocket 传输层协议。媒体侧协议为外部对接基准；Agent 侧负责兼容该协议，并在内部把 `chat` 文本请求转入 Gateway 和 assistant runtime。

## 1. 连接信息

- 协议：WebSocket
- 联调默认端口：`8089`
- URL：`ws://<agent_host>:8089/agent-service/v1`

本仓库本地服务默认端口是 `8000`。联调媒体服务时可用 `scripts/run_server.py --port 8089` 对齐上述端口。

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
| `error` | 未知消息或无法归属到具体 handler 的协议错误 | 参见 5 |

## 4. 消息体详细结构

### 4.1 assistantControl

发送 `client -> agent`：

```json
{
  "number": "用户号码",
  "callType": "AUDIO",
  "modelName": "模型名称(可选)"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `number` | string | 是 | 用户号码；无外层 `sessionId` 时也作为内部 session id |
| `callType` | string | 是 | `AUDIO` 或 `VIDEO` |
| `modelName` | string | 否 | 媒体侧希望使用的模型名称；Agent 当前记录但不直接用它绕过 provider/runtime policy |

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
| `stream` | boolean | 否 | 媒体侧流式标记；当前 Agent 返回一个完整 `chatResponse` |

处理规则：

- Agent 使用最新一条非空 `speechContent` 作为本轮 Gateway 输入文本。
- 只包含 `imageContent` 的内容项可以随请求传入，但当前不单独触发图像理解。
- `chat` 会进入 `GatewayTurnFacade -> GatewaySessionManager -> GatewayAgentAdapter -> AssistantRuntimeApp -> AgentGraphRuntime`。

响应 `agent -> client`：

```json
{
  "message": {
    "chatIndex": "对话索引",
    "content": {
      "intentResult": {
        "description": "AI回复文本",
        "status": "SUCCESS"
      }
    }
  },
  "display_only": false
}
```

外层示例：

```json
{
  "message": "chatResponse",
  "body": "{\"message\":{\"chatIndex\":\"chat-1\",\"content\":{\"intentResult\":{\"description\":\"你好，我可以帮你处理。\",\"status\":\"SUCCESS\"}}},\"display_only\":false}"
}
```

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

当前 Agent 将 `video` 作为传输层帧确认，不把原始视频内容送入 assistant prompt 或 provider。

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
Client                              Agent
  |                                   |
  |========== connect ===============>|
  |                                   |
  |======= assistantControl =========>|
  |<====== assistantControl ==========|
  |                                   |
  |============= chat ===============>|
  |<============ chatResponse ========|
  |                                   |
  |============= audio ==============>|
  |<============ audioResponse =======|
  |                                   |
  |============= video ==============>|
  |<============ videoResponse =======|
  |                                   |
  |=========== interrupt ============>|
  |<========== interrupt =============|
  |                                   |
  |============= close ==============>|
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
- `chat` 进入 Gateway 和 assistant runtime；`audio`、`video`、`interrupt` 当前返回传输层 ACK。
- 默认 mock/local/offline 运行不会调用真实外部 Provider；真实 Provider 只在显式 profile 和本机安全配置允许时启用。
- 不要在该接口中传输 API key、token、provider 原始响应或未脱敏敏感数据；原始音视频大 payload 不进入 prompt。
