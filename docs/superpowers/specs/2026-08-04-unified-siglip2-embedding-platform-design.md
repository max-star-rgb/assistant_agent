# 统一 SigLIP2 多模态表征能力设计

日期：2026-08-04
状态：已确认设计，待实施计划

## 1. 背景

项目当前把本地 `google/siglip2-base-patch16-224` 的 image encoder 与
`visual_projection` 导出为 ONNX，并在实时视频观察器中用于语义关键帧判断。这个实现证明了本地
视觉 embedding 的运行能力，但代码边界仍把 SigLIP2 表述为关键帧检测器的 image-only 私有依赖。

SigLIP2 的正确系统定位是统一的 image/text 跨模态表征能力。语义关键帧只是 image embedding 的
一个消费者；短期视觉记忆、文本与画面对齐、寻找物体和视觉关注也应复用同一模型 revision、同一
projection space 和同一推理结果。ASR 不构成第三种模态：语音在上游完成转写后，对本能力层只是
带来源和时间边界的 text observation。

本设计将模型推理、session 内计算治理和业务消费者拆开：模型能力统一，计算结果统一分发，业务
状态和策略由消费者分别拥有。

## 2. 目标与非目标

### 2.1 目标

- 提供统一、无状态的 image/text embedding Provider 和相似度校验契约。
- 保证 image tower 与 text tower 来自同一不可变模型 revision，并明确标识 embedding space。
- 在 session 内对同一 observation 的推理去重、调度、批处理并向多个消费者分发一次结果。
- 将现有语义关键帧迁移为第一个标准消费者，保持当前关键帧选择行为。
- 建立 session-scoped 多模态短期记忆，允许媒体重连后继续检索，并在 session 结束或 TTL 到期后清除。
- 一次性定义关键帧、短期记忆、图文对齐、寻找物体和视觉关注五类消费者的稳定边界。
- 保持现有 runtime、Tool、Provider mode、context、memory 和观测治理边界。

### 2.2 非目标

- 不把 SigLIP2 能力层扩展成承担推理、业务索引、意图判断和用户交互的全能中央服务。
- 不直接处理音频波形，也不新增 audio tower。
- 不把全局 image embedding 当作目标检测框或精确空间定位结果。
- 不自动把短期多模态记忆写入 conversation 或 Mem0。
- 不允许视觉关注消费者绕过 runtime 主动向用户发送消息。
- 不在 Runtime 中联网下载模型、tokenizer 或其他资产。

## 3. 核心原则

1. **统一空间**：只有 manifest 明确声明为同一 `embedding_space_id` 的结果才允许比较。
2. **逻辑无状态 Provider**：Provider 只做受配置约束的推理；进程级只读模型 session 缓存不构成业务状态。
3. **一次计算，多方复用**：同一 observation 的并发请求合并为一次推理，结果以不可变事件分发。
4. **计算治理与业务状态分离**：协调器只管理计算；索引、阈值、关联和 retention 都归消费者。
5. **结构化启用**：消费者和工具只依据可信 session、media、entry profile 和代码配置启用，不读取用户文本做关键词路由。
6. **as-of 因果边界**：查询只能使用请求到达前的 observation，后续视频帧不能污染本轮结果。
7. **失败关闭**：不得用零向量、不同模型空间、像素统计或 mock fallback 冒充真实跨模态语义。

## 4. 总体架构

```text
ImageObservation ─┐
TextObservation  ─┼─> SessionEmbeddingCoordinator
OnDemandQuery    ─┘       │
                            ├─ 请求合并、优先级、批处理、短暂去重
                            ├─ 调用 MultimodalEmbeddingProvider
                            └─ 发布 EmbeddingEvent
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           ▼
 KeyframeChangeConsumer    TemporalMemoryConsumer    CrossModalAlignmentConsumer
                                      │                           │
                                      ├──────────────┬────────────┘
                                      ▼              ▼
                             ObjectSearchConsumer  VisualAttentionConsumer
```

### 4.1 `MultimodalEmbeddingProvider`

Provider 是统一模型能力边界，至少暴露：

```text
embed_image / embed_images
embed_text  / embed_texts
readiness
```

Provider 不读取 session、视频流、memory、ToolRegistry 或用户意图。实现可以缓存不可变 ONNX Runtime
session 和 tokenizer，但每次调用结果只由输入、固定资产和显式配置决定。

Provider 返回成功结果或结构化失败，不抛出包含本机绝对路径、原始文本、媒体数据或底层 Provider
响应的公共错误。

### 4.2 `EmbeddingComparator`

Comparator 是与 Provider 并列的无状态公共组件，负责：

- 校验 `embedding_space_id` 相同；
- 校验 dimension 和 normalization contract 兼容；
- 计算 image↔image、text↔image 或 text↔text cosine similarity；
- 对空间不兼容、非有限向量和空向量返回结构化失败。

Comparator 不拥有 top-k、阈值、prompt template、时间衰减或置信度解释策略，这些属于消费者。

### 4.3 `SessionEmbeddingCoordinator`

每个 assistant session 最多拥有一个协调器。它负责：

- 按 observation identity 合并并发请求；
- 让交互式 on-demand query 优先于可延后的后台批次；
- 在 Provider 支持时执行有界 image/text batch；
- 保留有界 in-flight 和短暂结果缓存；
- 把不可变 `EmbeddingEvent` 分发给已注册消费者；
- 为每个消费者提供独立有界队列并隔离异常；
- 在 session 终止时停止接收、取消或有界排空，并清理临时状态。

协调器不创建语义索引，不保存“某物在哪里”等事实，不决定工具调用，也不把 embedding 写入 prompt。

### 4.4 标准输入与输出

`ImageObservation` 至少包含：

```text
session_id, video_id, connection_generation,
frame_id, sequence, captured_at_ms, image_ref
```

`TextObservation` 至少包含：

```text
session_id, observation_id, text,
source, occurred_at_ms, final
```

`source` 只记录可信来源，例如当前用户请求、已完成 ASR 文本或内部任务查询；它不改变模型处理方式。
当前系统只将稳定文本交给持久消费者。若未来入口产生 partial 文本，partial 只能用于可撤销的临时
匹配，不能形成稳定记忆。

成功输出使用 `EmbeddingEvent`，至少包含：

```text
event_id, modality, vector,
embedding_space_id, model_id, model_revision,
dimension, normalized,
session_id, source_observation_id,
video_id, frame_sequence, captured_at_ms,
text_source, occurred_at_ms,
latency_ms
```

图像专属和文本专属字段按 modality 可选。公共 trace 只投影身份摘要、状态、时延、队列和模型身份，
不记录 vector、原始文本、JPEG、绝对路径或 Provider 原始响应。

失败输出使用不含 vector 的 `EmbeddingFailureEvent`，携带 source observation identity、模型/空间身份、
安全错误码、recoverable 状态和时延。消费者不得把失败事件加入向量索引或相似度计算。

## 5. 模型资产与 readiness

联合 SigLIP2 manifest 必须固定：

```text
schema_version
model_id
model_revision
embedding_space_id
dimension
supported_modalities
normalization
image encoder/projection/input/output/checksum/preprocessing
text encoder/projection/input/output/checksum/tokenizer/max_length
```

image tower、text tower、projection 和 tokenizer 必须来自同一不可变 revision。manifest validation
必须校验文件存在、checksum、ONNX external data、输入输出名、数据类型、projection identity 和共同
embedding space。Runtime 不联网补全缺失资产。

迁移期允许 image-only manifest 显式声明 `supported_modalities=[image]`：

- 关键帧消费者可以继续工作；
- text/image 跨模态消费者报告 unavailable；
- 不允许静默选择另一 text model，也不允许仅凭相同维度视为同空间。

配置最终使用通用 embedding provider/model 命名；现有
`MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER` 与 `SIGLIP2_VISION_MODEL_DIR` 只作为有期限的兼容别名。
迁移后的首个发布周期只在新变量缺失时读取旧别名并记录 deprecated 配置状态；新旧值冲突时启动
失败。下一发布周期删除旧别名。具体目标版本号在实施计划读取当前版本配置后写明，不在运行时永久
维护两套事实来源。

## 6. 调度与结果分发

### 6.1 图像计算触发

不建立机械的固定 2 FPS 输出流。现有实时关键帧调度仍决定何时需要 image embedding：

- 首帧；
- SSIM 结构变化越阈值；
- semantic probe 到期，当前默认约每 500ms 提供一次保底语义检查机会；
- 最长关键帧间隔触发；
- 消费者对指定帧发起受限的 on-demand 请求。

每次实际完成的 image embedding 都发布为共享事件。semantic probe 是结构变化不明显时的保底检查，
不是严格的推理速率上限，也不等同于关键帧被选中。

### 6.2 文本计算触发

能力层只区分 image 与 text。Text observation 可以来自键盘输入、上游 ASR 转写或内部查询，但采用
惰性计算：只有至少一个结构化启用的消费者需要该文本时才执行 `embed_text`。普通对话不会因为经过
ASR 就被无条件写入多模态记忆。

寻找物体等交互查询使用即时 text embedding，并带 deadline 和较高优先级。协调器可以在不违反
deadline 的前提下批处理，不允许后台视频批次长期阻塞交互查询。

### 6.3 去重与背压

图像计算键由 `session_id + video_id + connection_generation + frame identity + embedding_space_id`
组成；文本计算键由 `session_id + observation_id + embedding_space_id` 组成。相同键的并发请求共享
同一个 in-flight future 和结果。

慢消费者使用独立有界队列。积压时由消费者声明 `latest_wins`、`drop_oldest` 或 `reject_new`
策略；任何消费者都不能阻塞媒体 ACK、Provider worker 或其他消费者。丢弃必须记录安全的结构化计数。

## 7. 消费者设计

### 7.1 `KeyframeChangeConsumer`

消费连续 image event，通过 Comparator 计算 image↔image 变化，输出当前
`semantic_change_score` 所需的结构化结果。现有 SSIM、组合阈值和最大间隔策略保持在关键帧模块，
不进入 Provider 或协调器。

迁移完成后，`SemanticChangeDetector` 不再私有加载 SigLIP2，也不维护另一份不可共享的模型推理路径。

### 7.2 `TemporalMemoryConsumer`

维护 session-scoped 的多模态时间线和 image vector index。它保存所有成功探测帧的向量和有界视觉
证据，而不只保存最终选中的关键帧，否则短暂出现的物体可能无法检索。

每条记录至少关联 session、video generation、frame sequence、capture time、image embedding、视觉
证据引用和可选 VLM semantic snapshot。向量数量、视觉证据数量、JPEG 总字节数、单 session TTL 和
全局 session 数分别设硬上限。淘汰后索引与证据引用必须原子删除，不能留下悬空路径。

该消费者按 session 生命周期存活；媒体临时断线和同 session 重连不清空记忆。session close 或 TTL
到期时删除全部向量、关联关系和 owned visual evidence。

### 7.3 `CrossModalAlignmentConsumer`

消费稳定 text event 和 image event，结合跨模态相似度与时间邻近性生成候选关联：

```text
alignment_score = consumer_policy(similarity, temporal_distance, source_confidence)
```

具体权重、时间窗和阈值属于消费者配置，并通过 eval 校准。关联结果是候选证据，不等同于用户意图、
事实确认或工具授权。

### 7.4 `ObjectSearchConsumer`

面向查询文本执行：

```text
query text embedding
  -> session image index top-k
  -> as-of 与 video generation 过滤
  -> 取回有界视觉证据
  -> VLM 对候选逐个或批量复核
  -> 返回已确认、候选、不确定或未找到
```

全局 embedding 只用于召回，不能单独证明物体存在或给出 bounding box。VLM 复核失败时不得把最高
相似度候选升级为“已经找到”。需要精确坐标时属于后续 grounding/detection 能力，不在本设计中伪装
实现。

面向主 LLM 的入口是受治理 `visual_memory_search` Tool。Tool 是否暴露只依据可信 session/media
capability；是否调用由主 LLM 决定，入口不得用关键词或正则路由。

### 7.5 `VisualAttentionConsumer`

保存当前已明确启用的关注目标 text embedding，并对新 image event 计算相关性和变化，只产出结构化
候选事件。它不能直接发送消息、创建 durable task 或修改 conversation。任何 proactive 行为必须进入
现有 runtime/durable task/interrupt 治理并获得相应授权。

## 8. 生命周期与 as-of 语义

```text
session create
  -> 创建 coordinator
  -> 按结构化 capability 注册消费者

media connect/reconnect
  -> 绑定新的 connection_generation
  -> 沿用同一 session temporal memory
  -> 拒绝旧 generation 的晚到结果覆盖新状态

user query
  -> 冻结 request-arrival frame boundary
  -> text embedding 与历史 index 检索
  -> 只允许 sequence <= boundary 的候选

session close / TTL
  -> 停止新请求
  -> 有界排空或取消 in-flight
  -> 清理消费者状态与视觉证据
  -> 释放 coordinator
```

重连可以延续同 session 记忆，但不同 connection generation 的 sequence 不能直接假设全局连续。排序
使用 generation、capture time 和该 generation 内 sequence 的组合契约。缺失或未来 capture time 不得
伪造年龄；as-of 判定必须优先使用入口冻结的可信 sequence/generation 边界。

## 9. 失败语义

- Provider 失败返回结构化 `EmbeddingFailure`，不产生零向量或假成功事件。
- 单个 observation 失败不清空历史成功结果，也不阻断其他帧或其他模态。
- 单个消费者异常被隔离并记录；协调器继续服务其他消费者。
- text tower unavailable 时，image-only 消费者可继续；跨模态消费者明确 unavailable。
- image tower unavailable 时，关键帧可按现有规则使用 SSIM，但 semantic score 必须 fail closed。
- 不同 embedding space 的比较必须失败，不允许自动转换或降级。
- VLM 复核失败时，搜索只能返回候选/不确定或可解释失败。
- session 已关闭、generation 已过期或 observation 超出 as-of 边界时，晚到结果不得发布给业务消费者。

## 10. 安全、上下文与治理

- 所有面向 Agent 的本地显式工具调用继续经过
  `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- Provider 和协调器是运行时依赖，不是绕过 Tool 治理的用户副作用入口。
- embedding、JPEG、媒体路径、原始文本和 Provider 原始响应不被动进入 prompt。
- 短期多模态记忆独立于 conversation summary、realtime task state 和 Mem0 snapshot。
- 只有用户明确要求并经过现有长期记忆生命周期时，才允许把经过文本化和审查的结论交给 Mem0；本
  设计不新增自动 promotion。
- trace 只保存模型身份、空间身份摘要、状态、时延、队列、去重和清理计数，不保存向量或媒体内容。
- mock mode 只使用确定性 mock/local 数据，不读取真实资产或发起网络调用；real mode 需要完整显式
  配置，不能因发现 key 或模型目录自动启用。

## 11. 可观测性

建议的结构化事件包括：

```text
embedding.requested
embedding.deduplicated
embedding.started
embedding.finished
embedding.failed
embedding.dispatched
embedding.consumer_dropped
embedding.session_cleanup
visual_memory.search.started
visual_memory.search.finished
visual_memory.verification.finished
```

主要指标：

- image/text 推理量、batch size 和各优先级队列深度；
- 同一 observation 重复推理率；
- Provider 与端到端 p50/p95 latency；
- 每个消费者成功、失败、丢弃和积压数；
- session 内向量、视觉证据和总字节数；
- session cleanup 后残留向量/JPEG 数；
- 检索 top-k、VLM 复核结果和 as-of 拒绝数。

观测事件必须沿用项目现有 redaction 和 run/session correlation 契约。

## 12. 实施拆分

统一设计通过四个顺序阶段实施：

1. **统一模型契约**：联合 manifest、image/text Provider、结果模型、Comparator 和配置迁移。
2. **session 协调器**：去重、优先级、批处理、结果分发和消费者隔离；先迁移关键帧消费者。
3. **多模态短期记忆**：temporal index、视觉证据 retention、跨模态时间关联和重连生命周期。
4. **功能消费者**：`visual_memory_search`、找物/VLM 复核和只产出候选事件的视觉关注。

阶段边界是验证与交付检查点，不代表建立四套并存架构。最终只有一个统一 Provider/Comparator、一个
session coordinator 和多个独立消费者。

实施完成后新增当前权威文档 `docs/multimodal-embedding-architecture.md`。同时更新 `AGENTS.md` 和
README 的任务路由，以及媒体、runtime、tool、context 文档中的集成边界。本 spec 是设计输入，完成
实施后不取代 `docs/*.md` 当前事实权威。

## 13. 测试与评测

### 13.1 pytest 决策

```text
Core invariant: unchanged.
Tests: 使用 tests/tdd/unified-siglip2 做临时 RED/GREEN；
模型质量和真实本地 CUDA 能力分别进入 system/Agent eval。
```

`tests/tdd/unified-siglip2/` 覆盖：

- joint/image-only manifest validation；
- image/text mock embedding 与同空间 Comparator；
- 不同空间、维度、非有限和空向量拒绝；
- 协调器同 observation 去重、交互优先级与有界 batch；
- 慢消费者和异常消费者隔离；
- generation、as-of、session TTL 和 cleanup；
- temporal index retention 与证据原子淘汰；
- 找物 top-k 与 VLM 复核失败语义。

现有 `tests/tdd/siglip2-keyframe/` 在迁移期间保护关键帧行为。两个 TDD feature 都是显式运行、可由
用户手动整目录删除的临时测试，不自动晋升 core。

当前设计不改变已登记 core invariant。若实施中实际改变稳定 Gateway session lifecycle，必须单独
说明 `GATE-001` 为什么变化，再评估扩展其既有 core 测试；不能因新增共享基础设施机械增加永久测试。

### 13.2 system eval

显式本地 system eval 验证：

- joint ONNX image/text 输出可用且属于同一空间；
- CUDA session 不回退 CPU；
- 固定输入输出可重复；
- 跨模态正样本排序高于受控负样本；
- image-only 资产只启用 image readiness；
- 资产缺失、checksum 错误和空间不匹配时 fail closed。

真实本地模型 eval 不放进 pytest，运行需要独立 operator 确认；Runtime 不在 eval 中联网下载资产。

### 13.3 Agent eval

Task 至少覆盖：

- 询问当前物体时正确使用实时视觉能力；
- 查询某物最后一次出现的时间或场景；
- 从未观察到目标时不编造；
- 相似物体或低置信候选时表达不确定性；
- 不消费用户查询到达后的未来帧；
- 普通聊天不调用视觉记忆工具；
- VLM 复核失败时不把向量候选说成确定事实。

主要质量和运行指标：

```text
跨模态 Recall@K / MRR
找物误报率
VLM 复核后的准确率
as-of 违规数 = 0
同一 observation 重复推理率 = 0
交互查询 p95 latency
后台消费者丢弃与积压数
session cleanup 残留向量/JPEG 数 = 0
关键帧选择回归差异
```

阈值和性能目标必须通过受控数据或真实运行证据校准，不能在实现中凭直觉写死为“准确”。

## 14. 验收标准

设计完成实现后应同时满足：

1. image/text 推理通过统一 Provider，并携带可验证的共同 embedding space identity。
2. 同一帧被多个消费者需要时只执行一次模型推理。
3. 现有关键帧策略不再私有加载模型，且行为回归在批准范围内。
4. session 内所有成功探测帧可进入有界 temporal index；媒体重连不丢失同 session 记忆。
5. session close 或 TTL 后不残留向量、关联关系或 owned JPEG。
6. 找物使用 text↔image 检索和 VLM 复核，并严格遵守 as-of 边界。
7. 视觉关注只产生候选事件，不绕过 runtime 主动产生副作用。
8. mock/real、Tool、context、memory 和 observability 治理边界没有旁路。
9. 临时 pytest、system eval 和 Agent eval 分别验证代码契约、真实本地能力与 Agent 行为质量。
