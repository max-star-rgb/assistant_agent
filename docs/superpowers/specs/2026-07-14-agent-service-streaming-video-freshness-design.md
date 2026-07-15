# Agent-Service 流式回答与实时视频新鲜度设计

日期：2026-07-14

## 目标

修复真实视频通话中的三个关联问题：Media 请求 `stream=true` 时 App 仍只能收到完整文本；实时视觉回答使用“你刚发送的视频”等上传式机械措辞；视觉提问可能把较早的后台语义快照当作当前画面。

本设计保持既有分层：App 只与 Media 交互，Media 通过 `/agent-service/v1` 与 Agent 交互。Agent 不实现 App 协议，也不绕过 Gateway、工具治理或 Media 的 ASR/TTS 边界。

## 当前根因

1. Agent-Service 固定写入 `response_streaming=false`，因此 native runtime 不为该入口注册可见文本 callback。`GatewayTurnFacade` 即使收到 `stream.chunk`，也只在 `run.end` 后返回拼接结果。Agent-Service 最终只发送一个 `chatResponse`。
2. Media 的 `chat.body.stream=true` 当前只影响响应 body 形状，没有控制响应投递方式。权威协议文档也明确记录“当前 Agent 返回一个完整 `chatResponse`”。
3. 实时请求仍渲染“附带视频 ID”，电话 prompt 没有把连续摄像头画面和上传视频区分开，模型容易生成“你刚发送的视频”。
4. 视觉请求只会等待完全没有成功快照的 `pending` 状态。已有旧快照且后台正在刷新时，状态为 `refreshing`，请求会直接消费旧结果。
5. `snapshot_age_ms` 使用 Qwen 结果发布时间计算，没有使用 Media 帧的采集时间，因此会低估模型实际看到的画面年龄。

## 协议与数据流

流式链路为：

```text
DeepSeek token delta
  -> AgentEvent(response_delta)
  -> RealtimeAgentEvent(response.chunk)
  -> Gateway stream.chunk
  -> GatewayTurnFacade chunk callback
  -> Agent chatResponse
  -> Media
  -> App 文本展示 / TTS
```

Media 发送 `chat.body.stream=true` 时，Agent 使用现有 `chatResponse` 消息类型发送多包：

- 中间包的 `intentResult.description` 是本包新增文本，`intentResult.status` 为 `PROCESSING`。
- 最终包的 `intentResult.description` 是完整回答，`intentResult.status` 为 `SUCCESS`。
- 中间包在 response body 顶层携带递增 `sequence` 和 `final=false`；成功终包在 response body 顶层携带最后序号和 `final=true`。这些字段不属于 `intentResult`；Media 用它们区分增量与终态。
- `deliveryId` 只出现在成功终包；`chatResponseAck` 只确认成功最终投递。流式失败以 `code=FAIL`、`final=true` 的无回答正文、无 `deliveryId` 终包关闭本轮 stream，该投递不进入 ACK 状态。
- `stream=false` 或缺省时保持单个完整 `chatResponse`，不改变旧客户端行为。
- 工具调用阶段的 provisional 文本继续受 runtime commit barrier 保护，不向 Media 泄漏可能被工具调用取代的模型前导语。

`GatewayTurnFacade.run_turn()` 增加窄范围的异步 frame/chunk consumer，而不是让 Agent-Service直接读取 Gateway endpoint。Facade 仍负责 run 注册、超时、取消和最终结果拼接，Agent-Service 只负责把已确认可见的 `stream.chunk` 包装成 Media 协议。

如果 Provider 未产生 token delta，Gateway 的最终回答兼容 chunk 仍只形成最终包，不能伪装为真实 token 流。Trace 应记录本轮是否看到了 provider token delta、发送的中间包数量和首包耗时，从而区分 Provider、Agent/Gateway 和 Media/App 三段责任。

## 自然口语与实时镜头语义

可信 `agent_service` 入口不再把 `video_ids` 渲染为“附带视频 ID”。视频引用仍留在结构化请求中，供 runtime 投影滚动快照，但用户消息改为表达“当前通话的实时镜头已连接”，或在已有独立实时视频区段时完全省略媒体 ID 行。

`realtime_phone` system prompt 增加以下风格约束：

- 把实时视频上下文理解为双方正在共享的当前镜头，不是用户上传或刚发送的视频文件。
- 视觉回答优先使用“我看到……”“看起来……”等自然指代表达。
- 不向用户提到视频 ID、快照、后台观察、Provider、上下文注入等实现细节。
- 画面证据不够新或仍在刷新时，简短说明“我看到的画面可能慢了一点”，不得把旧观察断言为当前事实。

该约束只影响可信实时电话 profile；普通视频上传/API 仍保留“附带视频 ID”和显式 `video_understanding` 行为。

## 视频新鲜度屏障

### 时间与序号

滚动快照增加并投影两类年龄：

- `frame_capture_age_ms`：当前成功语义快照所对应 Media 帧的采集年龄，是回答“模型看到多旧画面”的主要指标。
- `snapshot_publish_age_ms`：Qwen 结果发布后的年龄，用于判断缓存发布时间。

现有 `snapshot_age_ms` 迁移为帧采集年龄语义，避免继续低估陈旧度；诊断中保留明确命名字段，文档同步说明。若 Media 时间戳缺失或明显不可用，则采集年龄为 `null`，只报告发布年龄，不伪造时间。

### 视觉请求处理

视觉请求到达时，Agent-Service读取当前连接最近已解码帧的 sequence，形成 `target_sequence`：

1. 若成功快照序号已经达到目标序号，直接使用。
2. 若后台已有目标序号或更新序号正在执行/等待，则复用该任务。
3. 若最新帧尚未被自适应选择器排入队列，要求同一个 observer 把该帧提升为一次查询驱动的 latest-wins 候选。执行仍经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> video_understanding`。
4. observer 始终保持最多一个 Qwen in-flight 和一个 latest-wins pending 项，不从前台启动第二条 Provider 链路。
5. 视觉请求最多等待 4 秒，条件是出现 `snapshot_sequence >= target_sequence` 的成功快照，而不是等待全局 idle 或任意一次任务完成。
6. 超时后请求继续进入 DeepSeek，但上下文标记 `refreshing` 或 `stale`，包含安全的 sequence gap 和年龄诊断。旧 summary 不得被描述成当前确定事实。

普通问候、闲聊和不需要当前视觉事实的任务不触发 freshness barrier，也不额外排队 Qwen。

### 帧生命周期

查询驱动提升只使用 `VideoContextStore` 中当前连接的最新已解码 JPEG。observer 在保留关键帧时复制到自己的受控目录，因此不会依赖原始三帧窗口的后续淘汰。连接关闭仍统一取消 observer、清理语义状态和运行时文件。

## 错误与兼容行为

- Provider streaming 未启用、不支持或未产生 token delta 时，Agent 只返回一个成功终包；安全 trace 以 `provider_token_stream_seen=false` 和 `stream_chunk_count=0` 表达实际观测，不对 Media 声称存在 token 流。
- WebSocket 在发送中间包时断开，当前 delivery 标记 disconnected；不记录最终已发送或 ACK pending。
- 中间包发送失败会取消当前 Gateway run，避免后台继续生成不可投递文本。
- 最终文本仍以 Gateway terminal result 为准；当前诊断记录是否看到 Provider token delta、成功发送的中间包数量、首包耗时和成功终包是否已发送，不记录或比较正文。
- `stream=false`、旧 `assistantControlStart`、`chatProgress`、`chatResponseAck`、音视频 ACK 和最终响应 body 的既有字段保持兼容。
- 非 Agent-Service 视频上传/API 不受 freshness barrier 和实时措辞影响。

## 可观测性

`agent_service.turn.finished` 和相关 trace 增加安全字段：

- `stream_requested`
- `provider_token_stream_seen`
- `stream_chunk_count`
- `first_stream_chunk_latency_ms`
- `final_response_sent`
- `video.target_sequence`
- `video.snapshot_sequence`
- `video.sequence_gap`
- `video.frame_capture_age_ms`
- `video.snapshot_publish_age_ms`
- `video.freshness_waited_ms`
- `video.freshness_satisfied`

不得记录 token 文本、完整回答、用户原话、帧路径、Base64/Hex 媒体或 Qwen 原文。

## 测试策略

测试先行覆盖：

1. `stream=true` 的 fake streaming chat adapter 在第一次 DeepSeek 调用中产生多个 token delta，Agent-Service向 Media 发送多个 `chatResponse` 中间包和一个完整最终包。
2. `stream=false` 或缺省仍只发送最终包；ACK 只绑定最终包。
3. Provider 无 token delta 时不伪造中间 token 包。
4. tool-call preamble 不进入 Media，最终回答 token 在 commit barrier 后才发送。
5. trusted realtime request 不渲染“附带视频 ID”，system prompt 包含共享当前镜头和自然指代规则；普通上传入口保持原行为。
6. 视觉请求等待目标 sequence，而不是任意旧快照；4 秒内完成时使用目标快照。
7. 超时时只保留一个 Qwen in-flight 和一个 latest-wins pending，投影 sequence gap/stale 状态。
8. 普通问候不等待、不提升帧、不触发额外 Qwen。
9. `snapshot_age_ms`/`frame_capture_age_ms` 基于帧采集时间，发布年龄独立计算，缺失或未来时间戳安全处理。
10. Trace 诊断完整且不包含正文、路径或媒体数据。
11. Media 协议文档、Gateway 权威文档、Context Engineering 和 Observability 文档同步更新。

聚焦验证覆盖 Agent-Service WebSocket、Gateway facade/session/event mapping、native provider streaming、realtime video observer/memory、context renderer/report 和 latency trace；随后运行 `pytest -m fast -q` 与相关完整回归。真实 Provider smoke 仅在显式 `provider_smoke` 配置下执行普通问候和视觉请求，记录脱敏的首 token、最终响应、Qwen 次数和快照新鲜度证据。

## 验收标准

- Media 请求 `stream=true` 且 DeepSeek 支持流式时，在最终回答完成前收到至少一个正文增量 `chatResponse`。
- App 侧最终显示文本与最终 `chatResponse` 完整文本一致，Media 可按 `PROCESSING/SUCCESS` 和 `final` 区分增量与终态。
- 实时视觉回答不再出现“你刚发送的视频”或实现细节措辞。
- 视觉请求优先消费提问时最新帧对应语义；未在 4 秒内满足 freshness barrier 时不把旧快照伪装为当前事实。
- 每个连接仍只有一个 Qwen in-flight 和一个 latest-wins pending；前台 DeepSeek tools 中没有 `video_understanding`。
- 非实时视频工具、普通问候、多轮历史、取消/断开和旧客户端协议保持兼容。
