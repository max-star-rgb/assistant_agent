# 统一多模态 Embedding 架构

Last updated: 2026-08-05

本文档是 `assistant_agent` 当前 image/text embedding 平台、session 短期视觉时间线、历史找物和连接级视觉提醒能力的
事实权威。媒体接入与关键帧生命周期见 `media-agent-service-websocket.md`，显式 Tool 治理见
`tool-calling-architecture.md`；源码和测试与本文冲突时，以源码和测试为准并回补本文。

## 产品与工具边界

当前用户能力包括：

- 会话内短期视觉回忆：把选中的语义关键帧交给 VLM 文本化，并在 session 内保留成功结果；
- 历史找物：读取 Store 保留的最多 256 条带时间戳 VLM 文本；低于 Tool 输出预算 trigger 时完整交给
  主 LLM，达到 trigger 后由 Tool 尾部的 query-aware compactor 压缩旧 prefix，并保留相关原文与最近
  原文。Tool 不做 embedding、相似度排序或最终命中判定，也不在查询阶段再次调用 VLM。
- 连接级视觉提醒：把用户提交的视觉条件计算一次 text embedding，与每个已选关键帧的现有 image
  embedding 匹配，首次命中后通过当前 Agent-Service VIDEO 连接即时通知。

给主 LLM 的视觉语义 Tool 包括 `visual_memory_search` 和 `visual_reminder_manage`。后者只管理当前
可信 VIDEO 连接中的 `create/list/cancel`，不是 embedding Tool。`live_view_inspect` 继续回答当前实时画面，内部
`realtime_video_observe` 继续生成 rolling VLM snapshot。`siglip2_embed*`、`find_object`、
`visual_attention_manage` 都不是注册 Tool。Attention 仍只产生内部候选；连接级 reminder manager 是独立的
一次性状态机，不复用 Attention consumer。

VLM 推理层复用 Provider-neutral `VisionUnderstandingClient` 与 adapter：视觉 Tool 负责受信输入绑定、
Tool 治理和结构化结果，client/adapter 负责具体模型协议。同步 `media_inspect` 或显式视频调用在当前
Assistant trace 中形成 `tool.execute -> vlm.infer`；后台 `realtime_video_observe` 使用独立
`vision.observation` trace。embedding、视觉提醒和已有 VLM 文本检索不属于 VLM 推理，不经过该调用边界。

## 分层与数据流

```text
ImageObservation / TextObservation
        -> SessionEmbeddingCoordinator
        -> MultimodalEmbeddingProvider
        -> EmbeddingEvent | EmbeddingFailureEvent
        -> 独立有界 consumer queues（alignment / attention 等可选消费者）

Realtime frame
        -> 5 FPS fixed admission
        -> one image embedding per admitted frame
        -> SemanticKeyframeSelector
        -> selected image EmbeddingEvent
             ├─ VisualReminderManager（只比较 pending target，命中后即时 chatResponse）
             └─ current JPEG（单帧、无视觉历史）
                  -> parallel realtime_video_observe task per selected frame
                  -> VLM current-frame summary text
                  -> VisualSemanticRecord（带时间戳的单帧文本）
                  -> bounded timestamped text timeline
        -> SessionVisualSemanticStore
             ├─ live_view_inspect（最近 8 条 as-of 文本）
             └─ visual_memory_search（最多 256 条原始时间线）
                    -> VisualTimelineContextService（target / trigger / hard）
                    -> bounded Tool model_observation
```

Provider 发布模型、revision、dimension、normalization 和 `embedding_space_id`。Comparator 只有在
space、dimension、normalization、有限值和非零 norm 都兼容时才计算 cosine；不同 Provider 或未经
证明的图文空间不能混用。

Coordinator 按 session 隔离：相同 modality + observation id 的并发请求通过 `Future` 合并，成功结果
进入有界 LRU，失败只分发不缓存。每个消费者有独立有界队列和 overflow policy；慢或异常 consumer
不能阻塞其他 consumer。观测事件只记录摘要和 digest，不记录向量、文本、图片路径或原始标识。

## SigLIP2 资产与 readiness

schema v2 manifest 必须从同一不可变 `google/siglip2-base-patch16-224` revision 导出
`vision_model.onnx`、`text_model.onnx` 和 `tokenizer.json`，共同声明 `:joint-projection-v1` space。
image preprocessing 和 text tokenizer/padding/truncation/max length 都由 manifest 固定。两路输出均校验
dimension/finite 并 L2 normalize。ONNX Runtime 只允许 CUDA 为首个 execution provider，并关闭 CPU fallback。

schema v1 image-only manifest 仍可读取，但 readiness 必须是 `image_ready=true、text_ready=false`。
DashScope adapter 同样只声明 image readiness；没有共同空间证据时禁止拼接本地 SigLIP2 text。
mock 模式使用确定性离线共同空间，不加载真实模型。

新配置名是 `MULTIMODAL_AGENT_EMBEDDING_PROVIDER`、`SIGLIP2_MODEL_DIR` 和
`SIGLIP2_CUDA_DEVICE_ID`。旧 `MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER` 与
`SIGLIP2_VISION_MODEL_DIR` 是迁移 alias；新旧值冲突时启动失败。alias 计划不早于 `0.3.0` 删除，
删除前必须更新部署文档与迁移测试。

连接级视觉提醒使用 `REALTIME_VISUAL_REMINDER_SIMILARITY_THRESHOLD`（默认 `0.82`）、
`REALTIME_VISUAL_REMINDER_MAX_ACTIVE`（默认 `16`）和
`REALTIME_VISUAL_REMINDER_TERMINAL_HISTORY_LIMIT`（默认 `64`）。阈值只允许服务端配置，模型和用户
不能逐条覆盖；两个 limit 必须为正整数。主动消息后台投递使用
`PROACTIVE_MESSAGE_DELIVERY_TIMEOUT_SECONDS`（默认 `95` 秒），覆盖普通/视频 turn 的连接内串行等待，
同时为异常 channel send 提供有界终止。

## 全语义实时选帧

`semantic_input_fps` 默认是 5 FPS。固定时间准入后，每个准入帧只执行一次共享 SigLIP2 image
embedding；不再运行像素差、灰度指纹、SSIM、结构阈值或 combined score。旧
`REALTIME_KEYFRAME_SEMANTIC_PROBE_FPS` 仅作为 `REALTIME_SEMANTIC_INPUT_FPS` 的迁移 alias，显式配置
旧 structural/combined 参数会启动失败，防止部署误以为像素路径仍然生效。

实时性优先于逐帧完整处理：流水线最多一个 embedding in-flight 和一个 pending，pending 使用
latest-wins；因此不会积压，但高于处理能力的准入帧可以被更新帧替换。交互式 chat 目标可被 pin，
不能被后台帧替换。Selector 只根据当前 image embedding 与上一已选关键帧的 cosine distance、首次事件、交互提升
和最长 10 秒间隔选帧。semantic change 比较当前帧与上一已选 VLM 关键帧，使缓慢但累计明显的场景
变化仍能产生新记录；Provider 失败时只允许交互目标或最长间隔走降级 VLM，不伪造 semantic score。

提醒匹配只发生在 Selector 最终选中的帧上。`SemanticFramePipeline` 把同一次 image inference 产生的
`EmbeddingEvent` 交给 `RealtimeVideoObserver`，observer 不再计算 image embedding。embedding 失败后
因 interactive/max interval 降级选出的关键帧没有 event，因此跳过提醒匹配，但原有 VLM 流程仍可继续。

## 单帧文本时间线

后台 `realtime_video_observe` 只对选中关键帧调用 VLM。每次请求只有当前一张 JPEG，`memory_context`
固定为空；提示词要求模型只描述当前图片并返回非空 `summary`。每次 observation 新建独立 Qwen
WebSocket conversation；成功文本先发布到时间线，再在该帧独立清理阶段关闭连接，失败或不完整响应也会关闭，因此连续性不依赖 Provider 会话，也不由
主 LLM 选择图片窗口。

选帧后的每个关键帧立即建立独立 asyncio task，并在生产环境为该 task 新建 ToolRegistry、client、
adapter 和 Provider WebSocket；不同 sequence 并行执行，不共享可变 Provider 状态，也没有 observer 级
one-inflight/one-pending 队列。较新的关键帧可以先完成并发布；较早任务后续成功时只补入时间线，不回退
rolling latest snapshot。SigLIP2 选帧流水线仍保留一个执行中和一个 latest-wins pending，避免对所有输入帧
都调用 embedding/VLM。

连续性在 VLM 之后建立：每个成功结果成为带 `frame_sequence` 和 `captured_at_ms` 的
`VisualSemanticRecord`，并保留产生该文本的 `source_vision_trace_id`、`source_vision_run_id` 和
`source_vlm_span_id`。chat 到达 A 时刻时冻结此前最近的已选关键帧；`live_view_inspect` 只等待该 exact
sequence，完成即返回，不等待更早任务。随后它以该 sequence 为 as-of 边界，从 Store 读取最近 8 条已完成
记录，并向主 LLM 投影为按时间排序的 `[{timestamp_ms, text}]`。未来帧不能进入本次列表；
Store 自身的 retention 继续提供更大的有界历史；`visual_memory_search` 在可信 as-of/time window 内取
最后最多 256 条。原始记录不做预压缩；是否压缩只在 Tool 即将生成主 LLM observation 时按实际 token
预算决定。主 turn 的 `live_view_inspect` trace metadata 只关联本次实际选中 record 的来源 trace/span，
不能从并发 observation 的完成顺序反推。面向本机 loopback Langfuse 的 OTel 投影会额外生成
`source_vision_trace_url`，用于从 Tool observation 直接打开该来源 trace；它不是视觉记录或 Tool
结果的领域字段，远程 Langfuse host 不生成该 URL。

仓库中的 `VisualContextService`、视觉压缩配置与对应观测事件仍可供独立兼容代码和专项测试使用，但
当前 Agent-Service realtime observer 不构造、不调用它们，也不把 revisioned summary 或旧 record 文本
送入 VLM。

## 文本、ASR 与跨模态消费者

平台不直接处理语音。音频在上游转为稳定文本后，与键盘输入一样成为 `TextObservation`；`source`
只说明来源。Runtime 只对非空 final `request.text` 编码，且 session 必须存在 text consumer。文本
Runtime 的一般文本 embedding 不写入视觉语义存储或 Mem0。成功 VLM 结果的单帧文本直接进入 session
视觉时间线；`visual_memory_search` 不依赖记录是否成功建立 text embedding index。

Alignment 按 similarity 降序、时间距离升序关联同空间事件。Attention 只有设置内部 text target 后才
比较 image event 并保存 `visual_attention_candidate`；候选不会自动转为 Agent 行为。

创建提醒时，`visual_reminder_manage` 只把模型提交的视觉条件 `target` 编码一次；通知文案 `message`
不参与相似度计算。Manager 通过统一 `EmbeddingComparator` 校验图文 space、dimension、normalization、
有限值和非零 norm。一个关键帧可同时命中多条提醒，每条通过 `pending -> reserved -> triggered` 状态机
最多通知一次。命中后 Runtime registry 立即 dispatch 包含已保存 message 的 `ProactiveMessage`，后台
delivery task 负责 sink、超时和 confirm/release，不再次调用 LLM，也不阻塞后续 VLM 队列；
Agent-Service sink 只负责普通 chat 之后的 WebSocket 投影。单条 comparison 或发送失败不影响其他提醒
或 VLM。

## Session retention、as-of 与清理

`SessionVisualSemanticStore` 只发布通过校验的 VLM 成功结果，并把其 canonical text 编码为
`search_embedding`。Store 同时受记录数和 owned evidence 总字节限制；关键帧 evidence 通过 hard-link，
必要时退回 copy。淘汰、session/user 删除、TTL eviction 和 runtime pool close 都删除记录与 evidence。
普通 WebSocket transport close 不清理，因此同 user/session 重连仍能复用历史。
活跃实时 observer 同时持有 embedding coordinator 与 visual store lease；idle TTL 和 LRU 只淘汰无
lease 条目，避免长连接在持续处理期间被关闭。observer close 会幂等释放 lease；显式 session/user clear
和 runtime close 仍可立即终止并清理对应状态。

视觉提醒与上述 session retention 不同：它在成功 `assistantControl.callType=VIDEO` 后按内部
`runtime_session_id` 创建，切换同一连接的 `video_id` 时保留，WebSocket close 时立即关闭、清空和注销。
同一连接不允许重复 `assistantControl`，视频帧 `userNumber` 必须与握手 owner 一致。提醒创建还要求
SigLIP2 image/text 双模态 readiness 和 text event 的 model/revision/space/dimension 契约一致；不可用、
非归一化、非有限或零范数向量不会登记为 pending。提醒状态不写 `SessionVisualSemanticStore`、Mem0、
durable task 或 notification outbox，不能跨连接恢复。仅执行 `visual_reminder_manage` 的纯连接级 turn
还会依据结构化 ToolResult 确定性跳过 Mem0 ingestion；混合其他工具的 turn 不使用这条整体排除。
成功 server send 会在同一 runtime session 保存有界的 proactive session event，供下一轮主 LLM 理解
“知道了”等指代；该事件在连接关闭时清除，不进入 ConversationStore、Mem0 或跨连接恢复。

查询时 Runtime 绑定 user/session，ToolContext 提供可信 as-of sequence/time；模型只能提交
`query/time_window/search_mode`。Tool 不消费 query 做过滤、编码、相似度比较或排序，只按可信边界读取
Store 最后最多 256 条记录。读取后，Tool 尾部使用主 ChatAdapter 对应 tokenizer 和
`ContextWindowPolicy` 的 target/trigger/hard 心智模型：低于 trigger 原样返回；触发后保留最近原文，
并让专用 compactor 只用 indexes 选择与 query 相关的旧原文，同时生成 `timeline_summary` 和 coverage；
重建后再次计数。低于 hard 的压缩失败可带 `failed_below_hard` 返回原文，hard 区间重试仍失败则返回
`visual_memory_context_hard_limit`，禁止发送超限原文。Tool 不读取 evidence、不调用 VLM，也不输出路径、
向量或坐标。未来记录不能进入结果；状态为 `records|empty|unavailable`，其中 `records` 只表示存在可读
历史，不表示目标已经出现。

`visual_memory_search` 只读取原始 `VisualSemanticRecord`。Tool 尾部压缩结果只进入本次
`model_observation`，不反写 Store、conversation、Mem0 或后台 VLM 请求。主 `llm.context` tokenizer
preflight 仍对 conversation、memory、tools 与该 observation 合成后的完整 Provider request 执行第二级
hard gate。

## Tool 暴露与安全

`visual_memory_search` 是 `category=read`、`requires_media=[]`，视频断线后仍可查询已有历史。Runtime
在 catalog 构建前删除调用方传入的 `_trusted_visual_memory_available`，再根据当前 session Store 的
`SessionVisualSemanticStore.has_visual_history()` 覆盖；是否存在 text embedding index 不影响暴露，
exposure 也不检查请求关键词。执行仍经过
`ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。

`visual_reminder_manage` 是 `category=write`、`requires_media=[]`。Runtime 在 catalog 构建前删除调用方
传入的 `_trusted_visual_reminder_available`，只有请求来自可信 Agent-Service entry profile、结构化
`call_type=VIDEO` 且 owner/session registry 中存在活动 manager 时才覆盖为 true。Tool 的 session 由
runtime identity 注入；模型不能提交 owner、manager、embedding 或阈值。create/list/cancel 均经过同一
Validator、Executor 和 Registry 链路，exposure 不检查用户话术。

向量、owned evidence 路径和原始 VLM payload 不进入模型 schema、Tool data、trace、日志或 system eval artifact。
session evidence 不是长期记忆，不写 Mem0，也不跨 user/session 搜索。

## 验证入口

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-siglip2

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_multimodal_embedding_eval.py --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_agent_evals.py \
  --inspect --task visual_memory_last_seen_object
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_agent_evals.py \
  --inspect --task visual_memory_not_found_honesty
```

真实本地 CUDA 使用 `--allow-local-model`；真实 Chat/Judge 与 Langfuse publish/run 使用各自 operator gate。
pytest、dry-run 或检测到凭据都不得自动触发这些调用。

## 非目标

本期不提供语音 embedding、跨 session/长期视觉记忆、全局图库搜索、目标检测坐标、跨连接或重复视觉提醒、
离线提醒、自动任务、attention 管理 Tool、query-time VLM 复核，也不把 SigLIP2 当成完整 VLM。视觉提醒
命中只表示共同空间 cosine 达到服务端阈值，不升级为经 VLM 确认的事实。视觉塔做 semantic keyframe
只是统一 embedding 平台的一个消费者，不是平台本身。
