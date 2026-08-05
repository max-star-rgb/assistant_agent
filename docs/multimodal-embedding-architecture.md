# 统一多模态 Embedding 架构

Last updated: 2026-08-05

本文档是 `assistant_agent` 当前 image/text embedding 平台、session 短期视觉时间线和历史找物能力的
事实权威。媒体接入与关键帧生命周期见 `media-agent-service-websocket.md`，显式 Tool 治理见
`tool-calling-architecture.md`；源码和测试与本文冲突时，以源码和测试为准并回补本文。

## 产品与工具边界

本期新增的用户功能只有两个：

- 会话内短期视觉回忆：把选中的语义关键帧交给 VLM 文本化，并在 session 内保留成功结果；
- 历史找物：把用户查询与这些 VLM 文本放入同一 text embedding 空间排序，不在查询阶段再次调用 VLM。

新增给主 LLM 的 Tool 只有 `visual_memory_search`。`live_view_inspect` 继续回答当前实时画面，内部
`realtime_video_observe` 继续生成 rolling VLM snapshot。`siglip2_embed*`、`find_object`、
`visual_attention_manage` 都不是注册 Tool。Attention 只产生内部候选，不发消息、不创建任务、不触发工具。

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
        -> SigLIP2 keyframe
        -> current JPEG + VisualContextPack
        -> VLM current facts / changes / uncertainties
        -> VisualSemanticRecord
        -> raw-record search + next-call context projection
        -> SessionVisualSemanticStore
             ├─ live_view_inspect（当前/as-of 语义）
             └─ visual_memory_search（query text-to-record text 排序）
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

## 视觉上下文预检与压缩

后台 `realtime_video_observe` 只对选中关键帧调用 VLM，且每次请求始终只有当前一张 JPEG。启用
`VisualContextService` 时，请求的文本历史是一个固定 as-of 边界的 `VisualContextPack`：已有的旧
summary 加其后未覆盖的最近逐条 record 文本。每次 observation 新建独立 Qwen WebSocket
conversation，并在成功、失败或不完整响应后关闭，避免 Provider 隐式历史绕过本地预算。summary 是
带 revision 的最旧连续 record prefix；LLM schema 和语义投影不含 record ID，代码用有界 count、
sequence frontier 与固定 digest 计算 coverage。Store 只保存当前 raw retention 内的精确 covered ID，
所以迟到/同 sequence 新记录仍保持 uncovered，raw eviction 也不阻止 digest/frontier 后续扩展。压缩
只在成功、coverage 连续且 revision 未冲突时更新它，原始 `VisualSemanticRecord` 始终保留。

视觉预算复用 `ContextWindowPolicy` 的 target/trigger/hard 心智模型，但不复用主 Chat 模型的绝对
预算。独立 VLM tokenizer 对最终视觉历史、当前 query 以及 instruction/image/output reserve 做
preflight；target 选择预计使重建请求降到目标所需的最小最旧连续 prefix，并据目标剩余空间约束本轮
summary budget；配置的最近 records 始终保留。trigger 启动 LLM compactor，hard 是最终 Qwen/VLM
observation 调用前的拒绝边界。每次成功压缩后重建并重新计数，低于 hard 即可继续；最近 raw records
或 summary 使结果仍高于 target 时，不为追逐 target 无限压缩。CAS revision conflict 会重读同一
video/as-of 的 winning summary 并重建一次 pack，不使用 stale pack 决定 observation。Provider 不再对
该历史施加 4,000 字符截断。trigger 到 hard 之间压缩失败时保持旧 summary 和 raw records；hard 仍
无法收敛时跳过最终 Qwen/VLM observation，不阻塞视频 ACK，也不破坏 one-inflight/
one-latest-pending 调度。预算收敛期间独立 LLM visual compactor 可按现有状态机最多调用两次，不能把
“跳过最终 observation Provider”解释为此前绝无 compactor Provider 调用。

未启用 visual compaction 时，observer 才读取旧 rolling semantic snapshot 并使用最多 2,000 字符的
兼容输入，同时记录 compaction `unavailable`。该兼容 snapshot 不是 revisioned summary，不能被描述为
已启用的 VisualContextPack。

## 文本、ASR 与跨模态消费者

平台不直接处理语音。音频在上游转为稳定文本后，与键盘输入一样成为 `TextObservation`；`source`
只说明来源。Runtime 只对非空 final `request.text` 编码，且 session 必须存在 text consumer。文本
Runtime 的一般文本 embedding 不写入视觉语义存储或 Mem0；只有成功 VLM 结果的 canonical text 才建立
session visual search index。

Alignment 按 similarity 降序、时间距离升序关联同空间事件。Attention 只有设置内部 text target 后才
比较 image event 并保存 `visual_attention_candidate`；候选不会自动转为 Agent 行为。

## Session retention、as-of 与清理

`SessionVisualSemanticStore` 只发布通过校验的 VLM 成功结果，并把其 canonical text 编码为
`search_embedding`。Store 同时受记录数和 owned evidence 总字节限制；关键帧 evidence 通过 hard-link，
必要时退回 copy。淘汰、session/user 删除、TTL eviction 和 runtime pool close 都删除记录与 evidence。
普通 WebSocket transport close 不清理，因此同 user/session 重连仍能复用历史。
活跃实时 observer 同时持有 embedding coordinator 与 visual store lease；idle TTL 和 LRU 只淘汰无
lease 条目，避免长连接在持续处理期间被关闭。observer close 会幂等释放 lease；显式 session/user clear
和 runtime close 仍可立即终止并清理对应状态。

查询时 Runtime 绑定 user/session，ToolContext 提供可信 as-of sequence/time；模型只能提交
`query/time_window/search_mode`。Tool 只编码一次查询文本，并与同 embedding space 的记录文本向量做
cosine 排序；`object|scene|event` mode 会添加与 canonical VLM 文本字段一致的短前缀，`auto` 保留原
query。不读取 evidence、不调用 VLM，也不输出路径、向量或坐标。未来记录不能进入结果。状态固定为
`confirmed|candidate|not_found|unavailable`；query text embedding 失败返回 unavailable。

`visual_memory_search` 只索引和检索原始 `VisualSemanticRecord`。VisualContext summary 不进入 search
embedding、候选、排序或 as-of 过滤，也不进入主 Agent prompt、conversation 或 Mem0；它只在下一次
后台 VLM 调用前参与视觉历史投影。

## Tool 暴露与安全

`visual_memory_search` 是 `category=read`、`requires_media=[]`，视频断线后仍可查询已有历史。Runtime
在 catalog 构建前删除调用方传入的 `_trusted_visual_memory_available`，再根据现有 coordinator 的
`SessionVisualSemanticStore.has_searchable_history()` 覆盖；exposure 不检查请求关键词。执行仍经过
`ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。

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

本期不提供语音 embedding、跨 session/长期视觉记忆、全局图库搜索、目标检测坐标、主动提醒、自动任务、
attention 管理 Tool、query-time VLM 复核，也不把 SigLIP2 当成完整 VLM。视觉塔做 semantic keyframe 只是统一 embedding
平台的一个消费者，不是平台本身。
