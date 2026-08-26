# 实时视觉感知与语义关键帧架构

Last updated: 2026-08-25

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 与 Agent 运行框架解耦的视觉感知、低延迟关键帧文本化和语义关键帧算法权威 |
| Owns | SigLIP2 latest-wins、独立并行关键帧 VLM、逻辑关键帧窗口、目标帧实时屏障、视觉时间线、Qdrant 检索、历史找物、连接级视觉提醒与视觉 trace 语义 |
| Does not own | LangGraph/Agent Server 生命周期、Media-Agent wire、通用 Tool 执行链、长期记忆、VLM Provider 私有协议 |
| 源码与 schema 入口 | `src/assistant_agent/media/visual_perception/`、`media/embedding/`、`media/video/`、`tools/plugins/builtin/media_inspection/` |
| 验证入口 | `docs/authority.toml` 中 `visual-perception.verification` |
| 相邻 authority | 媒体 wire 见 [`media-agent-service-websocket.md`](media-agent-service-websocket.md)；Tool 集成见 [`tool-calling-architecture.md`](tool-calling-architecture.md)；部署资源见 [`agent-server-architecture.md`](agent-server-architecture.md) |

本文档是 `assistant_agent` 当前视觉能力的唯一事实权威。视觉流水线不依赖 LangGraph、自研 Runtime 或具体
Agent 编排框架：它接收已解码帧，独立完成低延迟语义关键帧、提醒、关键帧 VLM 和视觉文本存储，再通过窄接口供
Agent Tool 消费。框架迁移只能更换接入 adapter，不能绕过、合并或删除这里定义的视觉流水线。媒体 wire、
Agent Server 资源装配和标准 Tool 执行分别由相邻 authority 负责；源码和测试与本文冲突时，以源码和测试为准并回补本文。

## Visual Perception Module 边界

`VisualPerceptionModule` 是进程级视觉能力 owner；当前由 Agent Server lifespan 挂载，但公开契约不依赖
Agent Server 或 LangGraph，也不是第二套 Agent Runtime。它统一拥有 `VisionUnderstandingClient`/Provider adapter、`RealtimeVideoObserver`、
embedding coordinator、`SessionVisualSemanticStorePool`、视觉检索派生索引和连接级 session handle。
`RealtimeVideoObserver` 是模块内部的实时分析流水线，不与 Tool 或模块平级。

`uploaded_media_inspect` 为用户主动上传的图片/视频附件执行受治理的同步 VLM 推理，并复用模块持有的
进程级 `VisionUnderstandingClient`；它不读取摄像头实时视频。`live_view_inspect` 是实时视觉文本的
薄消费入口，不再为用户 query 二次调用 VLM。主 Agent LLM 根据模块已经发布的结构化文本回答 query。
实时读取分为两种语义：没有冻结目标窗口时读取 latest 已完成结果；Agent-Service chat 的 video block 携带
可信 `window_id + window_start_sequence + target_sequence` 时，最多 4 秒等待 exact target。strict 未命中
exact sequence 时旧记录只能用于状态诊断，Tool 内部结果记录 `usable_visual_text=false`，主模型不得把旧文本当作当前画面。
`live_view_inspect` 给主模型的成功或不可用结果都固定收窄为两个字段：`window` 按选帧顺序列出
`sequence + captured_at`，其中 `captured_at` 是 `Asia/Shanghai` 的 ISO 8601 时间；`vlm_response` 承载
该窗口的 VLM 语义文本或有界不可用说明。Provider、model、ready/missing、内部状态、引用和延迟只留在
Tool artifact、contract 与 trace，不进入模型可见 ToolMessage。

## 产品与工具边界

当前用户能力包括：

- 会话内短期视觉回忆：对 semantic selector 选中的关键帧执行 VLM 文本化，并在 session 内保留成功结果；
- 历史找物：在可信 user/session/as-of/time 边界内读取 Store 保留的最多 256 条带时间戳 VLM 文本，
  使用本地 Qdrant 的 multilingual BM25 与 `BAAI/bge-small-zh-v1.5` dense vector 做 Weighted RRF，
  BM25:dense 权重为 `3:1`、两路各 prefetch 32 条、返回最多 12 条。Tool 不在查询阶段再次调用 VLM，
  也不替主 LLM 做最终事实判定。
- 连接级视觉提醒：把用户提交的视觉条件计算一次 text embedding，与每个成功完成共享 image embedding
  的 semantic 准入帧匹配，首次命中后通过当前 Agent-Service VIDEO 连接即时通知。

给主 LLM 的视觉语义 Tool 包括 `live_view_inspect`、`visual_memory_search` 和
`visual_reminder_manage`。后者只管理当前
可信 VIDEO 连接中的 `create/list/cancel`，不是 embedding Tool。`live_view_inspect` 继续回答当前实时画面，内部
后台 observation service 继续生成 rolling VLM snapshot。`siglip2_embed*`、`find_object`、
`visual_attention_manage` 都不是注册 Tool。Attention 仍只产生内部候选；连接级 reminder manager 是独立的
一次性状态机，不复用 Attention consumer。

`visual_memory_search` 的模型可见描述明确它是当前 VIDEO 会话/thread 内的短期视觉记忆检索，不用于
跨会话长期视觉记忆。远端长期视觉记忆属于 Memory backend，由父图根据当前请求自动召回并以
`[长期视觉记忆]` 标记进入 `memory_context`；它不是视觉 Tool，具体契约由 Memory authority 所有。

VLM 推理层复用 Provider-neutral `VisionUnderstandingClient` 与 adapter：视觉 Tool 负责受信输入绑定、
Tool 治理和结构化结果，client/adapter 负责具体模型协议。同步 `uploaded_media_inspect` 调用在当前
Assistant trace 中形成 `tool.execute -> vlm.infer`；后台 observation service 使用独立
`vision.observation` trace。embedding、视觉提醒和已有 VLM 文本检索不属于 VLM 推理，不经过该调用边界。

## 分层与数据流

```text
ImageObservation / TextObservation
        -> SessionEmbeddingCoordinator
        -> MultimodalEmbeddingProvider
        -> EmbeddingEvent | EmbeddingFailureEvent
        -> 独立有界 consumer queues（alignment / attention 等可选消费者）

Realtime frame
        -> VisualPerceptionModule / connection session
        -> 进程级有界内存 frame index（JPEG 仍在连接级临时目录，断线清理）
        -> queue 1：one embedding in-flight + one latest pending raw frame
             -> SigLIP2 image embedding
             -> shared image EmbeddingEvent
             ├─ VisualReminderManager（每个成功 event 比较 pending target，命中后即时 chatResponse）
             └─ SemanticKeyframeSelector
                    -> selected / skipped decision
                    -> 按选中顺序组成最多 5 帧的半固定、互不重叠窗口
                         ├─ 满 5 帧：关闭窗口并立即启动 VLM task
                         └─ 任意 chat 到达 K：提前关闭当前 1～4 帧窗口并立即启动 VLM
                              -> 下一关键帧从新窗口开始
                              -> 每个窗口使用 isolated service/client/WebSocket（允许并行）
                              -> VLM window summary（最后一张是当前目标画面）
                         -> VisualSemanticRecord + bounded timeline + Qdrant derived index
        -> SessionVisualSemanticStore
             ├─ live_view_inspect（只读取本轮冻结窗口的 exact target）
             └─ visual_memory_search（最多 256 条可信候选）
                    -> Qdrant BM25 + BGE Weighted RRF（3:1，最多 12 条）
                    -> VisualTimelineContextService（必要时执行 target / trigger / hard）
                    -> bounded Tool model_observation
```

Provider 发布模型、revision、dimension、normalization 和 `embedding_space_id`。Comparator 只有在
space、dimension、normalization、有限值和非零 norm 都兼容时才计算 cosine；不同 Provider 或未经
证明的图文空间不能混用。

Coordinator 按 session 隔离：相同 modality + observation id 的并发请求通过 `Future` 合并，成功结果
进入有界 LRU，失败只分发不缓存。每个消费者有独立有界队列和 overflow policy；慢或异常 consumer
不能阻塞其他 consumer。观测事件只记录摘要和 digest，不记录向量、文本、图片路径或原始标识。

## SigLIP2 资产与 readiness

SigLIP2 只用于 image embedding、语义关键帧选择和 image-text 视觉提醒，不再承担 VLM 历史文本之间的
text-text 检索。历史文本检索的 dense encoder 与 SigLIP2 使用不同模型和索引边界。

schema v2 manifest 必须从同一不可变 `google/siglip2-base-patch16-224` revision 导出
`vision_model.onnx`、`text_model.onnx` 和 `tokenizer.json`，共同声明 `:joint-projection-v1` space。
image preprocessing 和 text tokenizer/padding/truncation/max length 都由 manifest 固定。两路输出均校验
dimension/finite 并 L2 normalize。ONNX Runtime 只允许 CUDA 为首个 execution provider，并关闭 CPU fallback。

schema v1 image-only manifest 仍可读取，但 readiness 必须是 `image_ready=true、text_ready=false`。
DashScope adapter 同样只声明 image readiness；没有共同空间证据时禁止拼接本地 SigLIP2 text。
mock 模式使用确定性离线共同空间，不加载真实模型。real 模式未显式选择并完整配置共同空间 Provider 时
必须返回结构化 unavailable，禁止回退 mock；只有 image/text readiness 同时成功才向生产 Tool composition
注入视觉提醒资源。local SigLIP2 readiness 除校验 manifest 外还必须成功初始化 CUDA-only image/text ONNX
backend，不能只凭资产文件存在宣称 ready。

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

视觉文本检索使用 `VISUAL_MEMORY_QDRANT_URL`（默认 `http://127.0.0.1:6333`）、
`VISUAL_MEMORY_QDRANT_COLLECTION`、`VISUAL_MEMORY_QDRANT_TIMEOUT_SECONDS` 和
`VISUAL_MEMORY_DENSE_MODEL_CACHE_DIR`。本地部署先显式安装 `.[visual-memory-search]`，再由 operator 在
可联网准备阶段把 `BAAI/bge-small-zh-v1.5` 下载到配置的 cache；运行时固定
`local_files_only=True`。Qdrant 可用以下 profile 单独启动，不会同时启动 Mem0：

```bash
docker compose -f docker/mem0/compose.yaml --profile visual-memory up -d qdrant
```

## 全语义实时选帧

每个成功解码的原始视频帧都尝试提交 SigLIP2；不再运行固定 FPS sampler、像素差、灰度指纹、SSIM、
结构阈值或 combined score。semantic 支路保持一个 embedding in-flight 和一个 latest pending；GPU 忙时，
尚未开始的旧 pending 被最新原始帧替换，优先限制延迟而不是补跑过时帧。旧
`REALTIME_SEMANTIC_INPUT_FPS`、`REALTIME_KEYFRAME_SEMANTIC_PROBE_FPS`、
`REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS` 以及 structural/combined 参数显式配置时启动失败，防止部署误以为
仍存在时间采样或选帧冷却 gate。

Selector 只根据当前 image embedding 与上一已选语义关键帧的 cosine distance、首次事件、交互提升和最长 2 秒
间隔选帧；第一帧始终以 `initial` 成为关键帧，semantic change 达到 `0.08` 时不再等待最小时间间隔，
静态画面通过 0.5 FPS 保底持续产生 selected event。
embedding Provider 失败时只允许交互目标或最长间隔形成无 embedding event 的降级选择，不伪造 semantic score。

`SemanticFramePipeline` 在 Selector gate 之前，把每个成功 image inference 产生的同一个
`EmbeddingEvent` 交给 `RealtimeVideoObserver` 执行 reminder comparison；随后 Selector 继续用该 event
计算相对上一已选关键帧的 semantic change。因此未被选中的原始帧仍可命中提醒，整条路径只调用一次
真实 image embedding 模型，不会为了提醒再次编码视频帧。单条提醒比较失败不会改变 Selector 决策；embedding
失败后因 interactive/max interval 降级选出的关键帧没有 event，因此跳过提醒匹配；关键帧 VLM 队列不依赖
reminder comparison 成功。

## 半固定关键帧窗口文本时间线

后台 observation service 只处理 Selector 已选中的逻辑关键帧。关键帧按选中顺序组成互不重叠、容量上限为 5
的半固定窗口；窗口满 5 帧或任意用户输入到达时立即关闭并发起一次多图 VLM。用户输入即使最终不触发视觉
Tool，也仍作为短期视觉记忆的分段边界。每次 observation 向同一个 Qwen realtime conversation 按时间顺序
append 该窗口的 1～5 张 JPEG，再统一 commit；`memory_context` 固定为空。提示词明确最后一张是当前目标画面，
前序图片只用于理解变化，`summary` 必须优先描述最后一张。成功文本发布后关闭该窗口的连接，失败或不完整
响应同样关闭，因此连续性不依赖 Provider 会话。

不同已关闭窗口各自创建独立 asyncio task、窄
`RealtimeVisualObservationService`、client、adapter 和 Provider WebSocket；它们允许并行，不经过全局 FIFO，
也不会等待、替换或取消其他窗口的 VLM。SigLIP2 选帧继续独立追踪最新画面，不受 VLM 速度影响。同一个
`(window_id, end_sequence)` 只执行一次；没有新关键帧的重复用户输入复用最近已关闭窗口，不重复推理。

媒体入口使用 `one in-flight + one latest pending` 消费解码消息，尚未开始的旧 pending 会被更新帧替换。chat
到达 K 时刻时在任何异步发送之前同步关闭 selector 当时已经登记的当前窗口，不等待媒体解码或未来关键帧：目标 k 是其中最后一帧，
窗口可包含 1～5 个逻辑关键帧。当时仍处于 pending/in-flight、尚未 selected 的帧不属于该窗口；K 之后首个
selected 关键帧开启下一窗口。即使 K+a 的 Tool 调用前新窗口已经增长，本轮仍只等待并读取 K 时刻已关闭窗口的
exact k 结果；Tool 不读取或理解选帧内部状态，也不回退到更早 sequence。

每个成功窗口成为带 `visual_window_id`、`window_sequences`、目标 `frame_sequence` 和
`captured_at_ms` 的不可变 `VisualSemanticRecord`，并保留产生该文本的 `source_vision_trace_id`、
`source_vision_run_id` 和 `source_vlm_span_id`。例如 K1 在第 3 帧到达、K2 在第 7 帧到达，随后继续选帧，时间线
依次得到 `[1,2,3]`、`[4,5,6,7]`、`[8,9,10,11,12]`，不会再生成 `[1,2,3,4,5]` 或 `[4,5,6,7,8]`。
较早窗口晚完成只补入历史，不修改已结束的 Graph run。Store retention 继续提供更大的有界历史；
`visual_memory_search` 在可信 as-of/time window 内取最后最多 256 条窗口文本。主 turn 的
`live_view_inspect` trace metadata 只关联本次 exact target record 的来源 trace/span，不能从并发 observation
完成顺序反推；领域结果不生成平台 URL。

仓库中的 `VisualContextService`、视觉压缩配置与对应观测事件仍可供独立兼容代码和专项测试使用，但
当前 Agent-Service realtime observer 不构造、不调用它们，也不把旧 summary 或 record 文本
送入 VLM。

## 视觉观测与 trace 契约

每个实际执行的窗口产生独立 `vision.observation -> vlm.infer` 路径。满五帧自动关闭的窗口使用
`window_role=background`，K 时刻触发的部分窗口使用 `window_role=target`；span 可记录 window ID、起止序号、
目标序号、关键帧数量与 `provider_connection_isolated=true`，但不记录 frame path、JPEG、VLM summary 或
Provider 原始响应。

chat 冻结目标边界后，`visual.target_barrier.started/finished` 才记录 window ID、起止序号、等待时长、
ready/missing 数量和目标终态。context 帧晚完成不能延长 target barrier span。LangGraph conditional edge 的
input/output 出现 `fast` 只代表 Agent 路由选择，视觉诊断必须定位真正的 `vlm.infer` generation；视觉流水线
本身不读取或依赖该路由结果。

semantic 诊断事件可记录当前 sequence、参考关键帧 sequence、image-image cosine、semantic change、阈值和
selected/reason；提醒诊断事件可记录脱敏 session/reminder ID、frame sequence、image-text cosine、阈值、
matched 和生命周期状态。它们不得记录目标文本、通知文案、embedding 向量、媒体内容、用户原始 ID 或媒体路径。
本地静态报告和实时报告的事件、曲线只投影这些允许字段，不读取或重算模型输入；日志中缺失的历史数值必须保持
缺失。实时报告可通过仅绑定回环地址的独立图片路由，仅为当前报告会话且当前日志快照已存在
`semantic_frame.selected` 的 session digest + sequence，从配置关键帧根目录下的
`semantic-input/agent-service-video-<session-hash-24>/frame-<sequence-8>-<uuid>.jpg` 读取 JPEG，在页面中显示
最新关键帧和最近 12 帧时间轴；目录路径、文件名与图片内容不得进入日志或 SSE，静态 HTML 不嵌入媒体。图片
路由必须拒绝无效 digest/sequence、目录或文件歧义、符号链接和根目录越界，并返回 `no-store`。实时模式先
建立当前日志快照，再以单调事件 ID 增量追踪追加内容；浏览器重连和日志轮转不得放宽字段 allowlist。实时模式
可显式固定 session digest；未固定时，以每个允许事件携带的有效 digest
自动切换到最近活跃视觉会话，切换时清空浏览器中的上一会话曲线和关键帧时间轴，禁止把多个会话的数据混画。

## 文本、ASR 与跨模态消费者

平台不直接处理语音。音频在上游转为稳定文本后，与键盘输入一样成为 `TextObservation`；`source`
只说明来源。Runtime 只对非空 final `request.text` 编码，且 session 必须存在 text consumer。文本
Runtime 的一般文本 embedding 不写入视觉语义存储或 Mem0。成功 VLM 结果的窗口文本直接进入 session
视觉时间线；`visual_memory_search` 只召回成功写入 Qdrant 派生索引的记录，并显式报告候选总数、
可检索数、实际返回数和 index coverage 是否完整。Qdrant 或本地 BGE 不可用时返回结构化
`unavailable`，禁止回退到 SigLIP2 text-text cosine。

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

`SessionVisualSemanticStore` 只发布通过校验的 VLM 成功结果；同一个完成 task 会立即把完整窗口文本以
`user_id + session_id + sequence + timestamp` 写入 Qdrant 派生索引，不等待更早 sequence 完成。
逐记录写入使用 Qdrant `wait=false`，以 WAL acknowledgment 结束发布热路径，避免 segment 优化长尾阻塞
该帧 task；Tool 查询前以本地时间线最后一条 record id 作为 freshness marker，最多 250ms 轮询 Qdrant
point retrieve。marker 可见后才执行混合检索；达到上限仍不可见时继续返回当前命中，但明确设置
`coverage_complete=false`。
查询前先由本地 Store 在可信 as-of/time 边界内得到每个 `(video_id, visual_window_id)` 的 canonical 最新
record IDs，并把这组 IDs 作为 Qdrant payload filter；重试或 legacy 重复记录不能占用排序候选后再被丢弃。
Store 不再保存用于历史检索的 SigLIP2 text embedding。Store 同时受记录数和 owned evidence 总字节限制；关键帧 evidence 通过 hard-link，
必要时退回 copy。淘汰、session/user 删除、TTL eviction 和 runtime pool close 都删除记录与 evidence。
普通 WebSocket transport close 不清理，因此同 user/session 重连仍能复用历史。显式 session/user 删除会
同时按严格 payload filter 删除 Qdrant points；Qdrant 是派生索引，本地 Store 仍是 retention 事实来源。
活跃实时 observer 同时持有 embedding coordinator 与 visual store lease；idle TTL 和 LRU 只淘汰无
lease 条目，避免长连接在持续处理期间被关闭。observer close 会幂等释放 lease；显式 session/user clear
和 runtime close 仍可立即终止并清理对应状态。

视觉提醒与上述 session retention 不同：它在成功 `assistantControl.callType=VIDEO` 后按内部
`runtime_session_id` 创建连接级 manager，但只有首个视频包成功解码并绑定 `video_id` 后才注册到 Runtime
registry；握手成功但尚无有效帧时 Tool 不暴露且 registry 查询不可用。切换同一连接的 `video_id` 时保留，
WebSocket close 时立即关闭、清空和注销。
同一连接不允许重复 `assistantControl`，视频帧 `userNumber` 必须与握手 owner 一致。提醒创建还要求
SigLIP2 image/text 双模态 readiness 和 text event 的 model/revision/space/dimension 契约一致；不可用、
非归一化、非有限或零范数向量不会登记为 pending。提醒状态不写 `SessionVisualSemanticStore`、长期记忆 backend、
durable task 或 notification outbox，不能跨连接恢复。仅执行 `visual_reminder_manage` 的纯连接级 turn
不会被独立 Memory Graph 的 `memory_extract` 当作跨 session 事实；混合其他工具的 turn 仍按正常提取策略处理。
成功 server send 会在同一 runtime session 保存有界的 proactive session event，供下一轮主 LLM 理解
“知道了”等指代；该事件在连接关闭时清除，不进入 ConversationStore、长期记忆 backend 或跨连接恢复。

查询时 Runtime 绑定 user/session，ToolContext 提供可信 as-of sequence/time；模型只能提交
`query/time_window/search_mode`。Tool 先按可信边界读取 Store 最后最多 256 条记录，再把 Store 最早保留
时间作为 Qdrant 下界，避免派生索引返回已经越过本地 retention 的旧记录。Qdrant 对 BM25 和 dense 两路
应用相同的 user/session/sequence/time filter，使用原生 Weighted RRF 返回最多 12 条。结果分别报告 `observation_count`、
`searchable_observation_count`、`matched_observation_count`、`returned_observation_count`、`truncated` 和
`coverage_complete`，不能用候选总数冒充模型实际收到的条数。若 Top-K 仍触发 Tool 输出预算，Tool 尾部
继续应用 `ContextWindowPolicy` hard gate；压缩后必须再次更新实际返回数和截断状态。Tool 不读取
evidence、不再次调用 VLM，也不输出路径、向量或坐标。未来记录不能进入结果；状态为
`records|empty|unavailable`，其中 `records` 只表示存在阈值内候选，不等同于最终事实确认。

每条返回给主 LLM 的 observation 保留机器字段 `timestamp_ms`，并根据同一可信帧时间确定性生成
`time_label`，同时给出相对查询时刻的时间和带 UTC offset 的 `Asia/Shanghai` 绝对时间。该标签只在
查询投影中生成，不写回 Store，不进入 VLM 窗口文本、Qdrant 文档或排序。未来时间戳只显示绝对时间，
避免产生负数相对时间。

`visual_memory_search` 只读取原始 `VisualSemanticRecord`。Tool 尾部压缩结果只进入本次
`model_observation`，不反写 Store、conversation、Mem0 或后台 VLM 请求。主 `llm.context` tokenizer
preflight 仍对 conversation、memory、tools 与该 observation 合成后的完整 Provider request 执行第二级
hard gate。

## Tool 暴露与安全

`visual_memory_search` 是 read Tool，但只在当前连接已完成 VIDEO 握手且可信
`user/session/as-of sequence` 已有可检索视觉文本时对模型可见；视频断线后不会继续暴露。生产 composition
在进程级视觉资源可用时静态构造该 `BaseTool`，再由统一条件 middleware 缩小每轮可见集合，不按请求关键词
建立动态 catalog。`live_view_inspect` 只有在本轮冻结投影已经包含 selector 选中的目标关键帧时才可见；
仅完成 VIDEO 握手、仅收到尚未成为关键帧的原始帧或冻结窗口为空时都不暴露；
`visual_memory_search` 的可检索历史判定以 backend-neutral 的 `index_status=ready` 为准；Qdrant 持有文本向量时，
不得再要求进程内 `VisualSemanticRecord.search_embedding` 非空。
媒体入口在创建 run 时冻结当前视觉投影，并通过 `Runtime.context` 只传递 server-issued opaque capability token；
条件 Tool 暴露和 Tool 执行必须以身份、thread、token 解析同一份投影，不得信任客户端提交的 video ID/sequence，
也不得在执行期重读可能已被后续聊天更新的 session 投影。冻结投影没有 target sequence 时历史 Tool fail closed，
不能把 `None` 当作无上界。
其描述把实时视频会话中的“这是什么/这个呢/它在做什么”等指示性问题视为视觉请求，不要求用户必须说出
“摄像头”或“画面”，但问候和无关纯文本任务不调用。实时画面是瞬时事实：每个新的当前画面问题都必须重新调用，
历史视觉 Tool observation 不能替代本轮证据；同一用户问题失败后不以相同参数重试。
如果工具暴露后因连接关闭或 capability 失效发生竞态，预期的 `ToolException` 必须由标准
`BaseTool.handle_tool_error` 转为有界 error `ToolMessage`，不得终止 Graph。
`uploaded_media_inspect` 只在最新用户消息含明确 `source=uploaded` 的图片或视频时可见。这三条条件与 Skill
渐进加载正交。执行经过标准
`BaseTool -> ToolNode` 路径，owner、session 与 as-of 边界由 `ToolRuntime` 注入，模型不可提交。
媒体连接登记 live-view projection 时使用与 Agent Server run 相同的认证 identity 和 thread ID；vendor
`userNumber` 只用于 wire 关联，不能作为 ToolRuntime 视觉资源 owner。

`visual_reminder_manage` 是 `category=write`。只有显式注入连接级 reminder resources 的受信 composition
才构造该 Tool；session 与身份由 `ToolRuntime` 绑定，模型不能提交 owner、manager、embedding 或阈值。
其 `availability=video_frame_received` 只读取服务端冻结 live-view projection 中非空的 `live_video_ids`，
不读取用户话术。create/list/cancel 经过同一 `BaseTool -> ToolNode` 路径，planning 模式下由原生 HITL
在执行前审批。

向量、owned evidence 路径和原始 VLM payload 不进入模型 schema、Tool data、trace、日志或 system eval artifact。
session evidence 不是长期记忆，不写 Mem0，也不跨 user/session 搜索。

## 验证入口

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/realtime-visual-target-window

MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_realtime_visual_target_window_eval.py --dry-run

MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_multimodal_embedding_eval.py --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_multimodal_embedding_eval.py --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
```

真实本地 CUDA 使用 `--allow-local-model`；真实 Chat/Judge 与 LangSmith publish/run 使用各自 operator gate。
pytest、dry-run 或检测到凭据都不得自动触发这些调用。

## 非目标

本期不提供语音 embedding、跨 session/长期视觉记忆、全局图库搜索、目标检测坐标、跨连接或重复视觉提醒、
离线提醒、自动任务、attention 管理 Tool、query-time VLM 复核，也不把 SigLIP2 当成完整 VLM。视觉提醒
命中只表示共同空间 cosine 达到服务端阈值，不升级为经 VLM 确认的事实。视觉塔做 semantic keyframe
只是统一 embedding 平台的一个消费者，不是平台本身。
