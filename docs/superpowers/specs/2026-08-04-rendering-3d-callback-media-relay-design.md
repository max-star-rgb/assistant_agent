# 3D 服务回调媒体转发设计

## 目标

`image_to_3d` 将受管图片提交给 3D 服务后，3D 服务通过
`POST /calling-agent-service/v1/{session_id}/{chat_index}/3d-gen-back` 返回产物。
Agent 校验回调并将产物转换为完整 `chatResponse`，通过发起本轮请求的媒体 WebSocket 连接发送给
媒体服务。

## 边界

- 回调投递是入口层的异步媒体中继，不创建新的 Agent turn，不调用 LLM，也不复制主运行时。
- 3D 产物只以 URL 形式转发；Agent 不下载、缓存或解析模型、视频及可选预览图。
- `image_to_3d` 仍经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- 只支持当前进程中的活动媒体连接，不引入持久队列或跨进程消息系统。

## 数据流

1. `/agent-service/v1` WebSocket 建立后，以 runtime-owned `session_id` 注册活动发送通道，并保留该连接
   当前的 `userNumber`；断连时注销。
2. 当前 `chatIndex` 通过可信的 `agent_service` request metadata 进入 `ToolContext`。
3. `image_to_3d` adapter 使用 runtime `session_id` 和当前 `chatIndex` 构造 `pre_cb_url`、`cb_url`，不再
   固定使用路径值 `0`。
4. 回调校验 `mediaType`、HTTP(S) `mediaUrl`，然后按 runtime `session_id` 查找原媒体连接。
5. 回调构造 legacy 完整 `chatResponse`，其 `body` 保持 JSON 字符串，并复用回调路径中的
   `chat_index`：
   - `ply`、`glb`：`{"type":"TD_MODEL","modelUrl":"<mediaUrl>"}`
   - `mp4`：`{"type":"VIDEO","videoUrl":"<mediaUrl>"}`
6. WebSocket 发送成功后返回
   `{"errCode":0,"errMessage":"success","data":{"result":"SUCCESS"}}`。

`intentResult.description` 固定为“小艺已经为您生成3D蛋糕模型”，其余字段采用已给出的完整协议结构：
`messageType=ANSWER`、`display_only=false`、空 `intentExecution`/`intentWeb` 和单项 `detail`。

## 失败语义与并发

- `mediaType` 仅接受 `ply | glb | mp4`；缺少或非法字段由 HTTP schema validation 拒绝。
- 找不到活动 session、连接已经关闭或 WebSocket 发送失败时，不返回成功 ACK，使用非 2xx 响应让
  上游感知未投递。
- WebSocket 回调投递复用连接现有的 `send_lock`，避免与普通 `chatResponse` 并发写入。
- 注销操作按连接身份匹配，避免旧连接关闭时误删同 session 的新连接。
- 日志只记录脱敏 session 标识、媒体类型和投递结果，不记录完整 URL、Base64 或用户内容。

## 验证

- adapter 回调 URL 包含真实 `chatIndex`。
- `ply`、`glb`、`mp4` 分别映射到正确的 detail 类型和 URL 字段。
- 回调产生完整的双层 JSON `chatResponse`，手机号和 `chatIndex` 正确。
- 无活动连接、断连清理和发送失败不会返回成功 ACK。
- 既有图片投递、3D 提交与媒体聊天测试继续通过。

