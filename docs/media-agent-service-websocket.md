# Media-Agent WebSocket 接口权威文档

Last updated: 2026-08-13

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Media-Agent WebSocket envelope、消息字段与当前兼容面的协议权威 |
| Owns | `/agent-service/v1` 的 assistantControl、chat、audio/video ACK、interrupt、chatResponseAck 与 3D callback wire |
| Does not own | Agent Server thread/run/checkpoint、Assistant 推理、Tool/Memory 策略和媒体服务内部实现 |
| 源码与 schema 入口 | `src/assistant_agent/agent_server/media_*.py`、`src/assistant_agent/api/rendering_3d_callback.py` |
| 验证入口 | `docs/authority.toml` 中 `media-agent-protocol.verification` |
| 相邻 authority | Agent Server 部署见 `gateway-architecture.md`；视觉能力见 `multimodal-embedding-architecture.md` |

## 1. 连接与 envelope

- WebSocket：`ws://<agent_host>:<port>/agent-service/v1`
- 默认本地端口：`8000`；媒体联调可用 `scripts/run_server.py --port 8089`。
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

首个业务消息应为 `assistantControl`：

```json
{"number":"user-1","callType":"AUDIO"}
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

最终正文来自 native state 的 `assistant_state.final_response.message`，适配器不得从 delta 拼接或自行生成
业务回答。Graph 的 `publish_response` 已先发生，随后 `memory_commit` 完成，run 才到达终态。

## 4. interrupt 与 delivery ACK

`interrupt` 会对该连接已关联的 run 调用 Agent Server 原生 cancel，然后返回：

```json
{"code":0,"message":"interrupted"}
```

`chatResponseAck` body 必须同时携带匹配的 `chatIndex` 和 `deliveryId`；成功返回
`{"code":0,"message":"acknowledged","deliveryId":"..."}`。ACK 不改变 Graph/run 终态，也不触发长期
记忆提交。

## 5. audio、video 与 3D callback

当前 custom route 对 `audio`、`video` 只做传输层 ACK，分别返回 `audioResponse`、`videoResponse`；尚未把
媒体 payload 摄取为 Graph 输入或实时视觉上下文。旧实现中的 H.264 解码、视觉 observer、图片/3D 主动
WebSocket 投递尚未迁入原生 route，不能视为当前受支持能力。

保留 callback：

```text
POST /calling-agent-service/v1/{job_or_session_id}/{chat_index}/3d-gen-back
```

它记录中性 3D job artifact；不执行 Assistant Graph。callback 的 delivery hub 仍是过渡组件，后续应迁移到
Agent Server 可持久通知资源，不能据此宣称跨进程可靠交付。

## 6. 错误与认证

- 非 `v1` 连接返回失败并以 1008 关闭。
- 非法 JSON body、缺字段、identity mismatch、重复关联或未知消息返回结构化失败 envelope。
- custom route 启用 Agent Server auth。内部 thread/run 调用走同源公开 API 并只转发 Authorization header，
  使 Graph factory 能从 `ServerRuntime.user` 校验 context delegation；不得使用 `/noauth` 后信任客户端 context。
- mock mode 使用本地 developer principal；real mode 要求显式 `ASSISTANT_AGENT_SERVER_SERVICE_TOKEN`。

## 7. 重连与当前限制

Agent Server 原生 thread/run/stream 支持 SDK join/resumable stream，但当前媒体 connection 尚未实现 vendor
重连后恢复旧 thread/run 的握手字段。因此当前断线重连会创建新 thread，不自动重放未 ACK delivery，也不自动
恢复上一条 chat。实现这项能力时应只保存最小 vendor-to-native correlation，并使用
`threads.join_stream(last_event_id=...)`；不得重建 Gateway session/runtime。

当前还未迁移：并发 chat task consumer、周期 progress、citation/图片/detail 增强包、durable task/workflow
主动通知、实时 H.264/视觉观察和断线 delivery outbox。媒体服务在采用本版本前必须按本节核对能力，而不能仅
依据 URL 未变化判断完全等价。

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
