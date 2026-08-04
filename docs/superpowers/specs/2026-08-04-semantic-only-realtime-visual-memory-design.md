# 全语义实时视觉与短期语义记忆设计

日期：2026-08-04
状态：已批准；基础全语义架构已实施，视觉上下文压缩待实施计划

## 1. 背景与目标

当前实时视频链路同时使用像素差、SSIM 和 SigLIP2 embedding 参与采样与关键帧判断，短期视觉记忆则保存
采样画面的 image embedding，并在查询时再次调用 VLM 复核。该设计存在三类问题：

- Pixel Diff 和 AdaptiveSampler 位于昂贵计算之后，不能实际减少 embedding 推理；
- SSIM 既调度 semantic probe 又直接决定关键帧，与统一 embedding 平台的职责重叠；
- 实时 VLM 理解和历史找物使用两份事实来源，查询链路长且可能产生两次 VLM 结论不一致。

本设计将视频处理改为固定帧率、全语义、有界 latest-wins 流水线，并将短期视觉记忆定义为“已成功完成
VLM 理解的关键帧语义记录”。目标优先级依次是：实时无积压、事实来源统一、session 隔离与可解释查询。

## 2. 已确认边界

- 媒体协议可声明约 30 FPS，但 Agent 视觉语义入口默认固定为 5 FPS。
- “每帧 embedding”指每个被时间采样器接纳并实际进入视觉管线的帧，不指媒体原始 30 FPS 全量帧。
- 时间采样只使用单调时钟和帧序列，不读取像素内容。
- 删除 Pixel Diff、SSIM 和基于二者的 AdaptiveSampler。
- 每个接纳帧都调用统一 `SessionEmbeddingCoordinator` 生成 SigLIP2 image embedding。
- 关键帧只依据 image embedding 的 semantic change、最小间隔和最大间隔决定。
- 只有成功完成 VLM 理解的关键帧才进入短期视觉记忆。
- `live_view_inspect` 与 `visual_memory_search` 读取同一个视觉语义事实源。
- 历史查询不再进行第二次 VLM 复核。
- Mem0 不承载 session 短期视觉记忆，也不自动接收视觉记录。
- 主 LLM 仍只新增/保留 `visual_memory_search` 一个历史视觉 Tool；embedding、采样、attention 和后台观察
  都不是主 LLM Tool。

## 3. 术语

- **媒体原始帧**：Media-Agent 每条可独立解码的 H.264 I-Frame 消息。
- **语义入口帧**：通过固定时间采样后，被接纳进入 embedding 流水线的帧。
- **语义关键帧**：image embedding 相对上一 VLM 关键帧发生足够语义变化，或满足首帧/最大间隔规则的帧。
- **视觉语义记录**：关键帧完成 VLM 理解后形成的、经过 schema 校验的结构化事实。
- **当前画面记录**：不晚于本轮可信视觉边界的最新成功视觉语义记录。

## 4. 总体数据流

```text
Media-Agent H.264 frames (~30 FPS)
        -> decode + VideoContextStore
        -> FixedIntervalSemanticSampler (default 5 FPS)
        -> SemanticFrameQueue
             - one in-flight
             - one pending slot: latest-wins background or pinned interactive
        -> SessionEmbeddingCoordinator.embed_image
        -> EmbeddingEvent | EmbeddingFailureEvent
             -> CrossModalAlignmentConsumer
             -> VisualAttentionConsumer
             -> SemanticKeyframeConsumer
        -> selected semantic keyframe
        -> existing governed realtime_video_observe / VLM queue
        -> validated VisualSemanticRecord
        -> SessionVisualSemanticStore
             -> latest/as-of read for live_view_inspect
             -> bounded historical search for visual_memory_search
```

视频 ACK 在帧完成校验、解码、写入最近帧上下文并完成语义队列 admission 后立即返回，不等待 SigLIP2 或
VLM。ACK 表示帧已被安全接收和调度，不表示该帧最终完成 embedding、成为关键帧或产生 VLM 记录。

## 5. 固定时间采样与过载控制

`FixedIntervalSemanticSampler` 使用服务端单调时钟控制 admission，默认间隔为 200ms。媒体声明的
`frameRate` 只用于诊断，不作为可信调度时钟。序列倒退或重复的帧不得进入流水线。

后台 semantic queue 始终最多包含：

- 一个正在执行的 image embedding；
- 一个尚未执行的 pending slot：通常保存 latest-wins background；chat 到达时可改为 pinned interactive。

正在执行的 ONNX 推理不强行取消。替换只发生在尚未开始的 background pending，因此不会产生无界排队。
被时间采样跳过或被 latest-wins 替换的帧不生成 embedding、不进入短期视觉记忆，并记录安全计数而不记录
图片、路径或向量。

## 6. 当前画面 interactive 优先级

Agent-Service 收到 chat 时冻结不晚于 chat 到达边界的最新原始帧。若该帧还没有成功 embedding：

1. 创建 pinned interactive semantic job；
2. 它替换普通 background pending，但不取消正在执行的 job；
3. pinned job 不得被后续视频帧替换；
4. embedding 完成后，无论 semantic change 是否超过后台阈值，都将它送入 VLM；
5. 直到对应 snapshot 成功、失败或本轮等待结束后才释放 pin。

同时只允许一个 chat turn 持有 interactive pin。interactive job 执行期间可以保留一个最新 background
pending，但不能形成第三层积压。查询不能消费 chat 边界之后的帧或语义记录。

## 7. 纯语义关键帧策略

关键帧输入只有兼容且归一化的 image `EmbeddingEvent`。相似度由统一 `EmbeddingComparator` 计算，语义变化
分数为 `1 - cosine_similarity`。规则按顺序为：

1. 首个成功 embedding 帧成为关键帧；
2. 距上一 VLM 关键帧小于最小间隔时不进入后台 VLM；
3. semantic change 达到配置阈值时成为关键帧；
4. 达到最大间隔时，最新已接纳帧强制刷新 VLM；
5. 其余帧只产生 embedding event，不进入 VLM。

最大间隔属于实时可用性规则，不伪装成 semantic change。若 image embedding 失败，失败帧不写向量记录；
达到最大间隔时仍允许将最新可用 JPEG 送入 VLM，以避免 rolling snapshot 永久停滞。

## 8. 统一视觉语义记录

新增 `VisualSemanticRecord` 公共契约，至少包含：

```text
record_id
session_id
video_id
frame_sequence
captured_at_ms
scene
objects
people
actions
events
text_in_video
summary
search_embedding
embedding_space_id
index_status
evidence_ref
created_at_ms
```

只有 VLM 调用成功、结果 schema 有效、来源为后台关键帧观察时才创建记录。Provider raw response 不保存；
只保存归一化后的结构化字段。`search_embedding` 可以为空；`index_status` 固定为 `ready|unavailable`。
可检索记录的 `search_embedding` 由规范化检索文本通过同一 SigLIP2 text tower 生成：

```text
场景：<scene>
物体：<objects>
人物：<people>
动作：<actions>
事件：<events>
文字：<text_in_video>
摘要：<summary>
```

检索文本和向量不进入主 LLM schema、日志或 trace。Tool 返回结构化字段，不返回内部拼接文本、向量、绝对
路径或 VLM 原文。

## 9. SessionVisualSemanticStore

`SessionVisualSemanticStore` 是媒体 runtime 的 session 级短期状态，不属于长期 Memory 服务。它统一承担：

- 最新成功记录：供 `live_view_inspect` 按可信 target sequence 读取；
- 有界历史记录：供 `visual_memory_search` 按 query、时间窗口和 as-of sequence 检索；
- session-owned evidence：为记录保留同文件系统 hard-link；
- 生命周期清理：session/user 删除、TTL eviction、runtime pool close 时删除记录、向量和 owned link。

Store 默认最多保留 256 条记录、256 MiB session-owned evidence，并与 coordinator store 使用相同的
1800 秒 idle TTL。普通 transport 短暂断开不删除仍由 runtime session 拥有的记录；明确 session/user 删除和
runtime pool eviction 必须清理。最新 rolling snapshot 从同一组成功记录派生；历史查询只读取其中
`index_status=ready` 的记录，不再维护与历史查询分离的第二份语义事实。

Runtime 使用 `SessionVisualSemanticStorePool` 按可信 `(user_id, session_id)` 创建、复用和清理 store。observer、
`live_view_inspect` 与 `visual_memory_search` 必须从同一 pool entry 取得同一 store；opaque `video_id` 不能替代
user/session 身份隔离。pool 的 TTL、最大 session 数、clear_session、clear_user 和 close 语义与 coordinator
store 对齐。

## 10. visual_memory_search

Tool 的名称、只读类别、runtime session 绑定和可信 exposure 规则保持不变。内部查询改为：

1. Runtime 注入可信 `session_id`、`as_of_sequence` 和 `as_of_ms`；
2. 用 SigLIP2 text tower 编码用户 query；
3. 过滤当前 session、时间窗口和不晚于 as-of 的记录；
4. 只比较兼容 text embedding space 的记录；
5. 按 cosine similarity 排序并应用可配置的最小相似度；
6. 返回有界 top-k 结构化 VLM 事实和采集时间；
7. 不再调用查询时 VLM。

状态继续使用 `confirmed|candidate|uncertain|not_found|unavailable`：高于确认阈值的成功 VLM 记录为
`confirmed`，介于候选阈值与确认阈值之间为 `candidate`，低于候选阈值为 `not_found`；text embedding
不可用为 `unavailable`。`uncertain` 保留为 schema 兼容状态，本设计的文本检索路径不主动产生它。阈值必须
由固定配置给出并通过离线正负样例校准，禁止根据用户话术动态改写。

历史记录只能证明物体或场景在过去画面中出现，不能证明当前位置、持续存在或精确坐标。

## 11. Mem0 边界

Mem0 继续只处理跨 session 长期用户事实：session 创建时冻结召回，最终对话完成后异步提交原始
user/assistant messages。视觉语义记录不得直接提交 Mem0，原因包括：

- 当前 Mem0 接口没有视觉 session 所需的即时写后读、TTL、严格 as-of 和 evidence 生命周期；
- Mem0 inference 可能提取、合并或改写文本，不能保持逐帧事实顺序；
- 当前 `custom_instructions` 明确忽略临时视觉环境；
- mock/offline 模式下 Mem0 不可用，不能成为实时视觉功能的必需依赖。

如果用户在正常对话中明确把某个视觉事实提升为跨 session 长期事实，例如确认备用钥匙长期放在固定位置，
仍由现有 turn capture 把 user/assistant messages 提交 Mem0。项目不实现自动视觉 promotion，也不新增
memory Tool。

## 12. 组件迁移

- 以 `SessionVisualSemanticStore` 取代当前分离的 `RealtimeVideoMemoryStore` rolling state 与
  `TemporalVisualMemory` image-vector timeline；实施完成后不得保留两份可被查询的视觉事实。
- `RealtimeVideoObserver` 在 VLM 成功后负责规范化并发布 `VisualSemanticRecord`，不直接维护另一份
  `current_state` 文本。
- `VisualMemorySearchService` 移除 `VisionUnderstandingClient` 依赖，只依赖 session store、text embedding
  coordinator 和 comparator。
- Runtime 不再注册 `TemporalMemoryConsumer`，coordinator 不再挂载 `temporal_visual_memory` 动态属性。
- `CrossModalAlignmentConsumer` 与内部 `VisualAttentionConsumer` 可以继续消费语义入口帧的 image
  embedding，但它们不拥有短期视觉事实，也不改变主 Agent 行为。
- 当前架构权威文档、媒体协议说明、Tool 治理、context 和 observability 文档在实现同一变更中同步更新；
  本设计文件仍属于开发阶段材料，不替代 `docs/*.md` 当前权威。

## 13. 配置迁移

新 canonical 配置：

- `REALTIME_SEMANTIC_INPUT_FPS=5`
- `REALTIME_KEYFRAME_SEMANTIC_THRESHOLD=0.18`
- `REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS=0.5`
- `REALTIME_KEYFRAME_MAX_INTERVAL_SECONDS=10`
- `REALTIME_VISUAL_MEMORY_CANDIDATE_SIMILARITY=0.20`
- `REALTIME_VISUAL_MEMORY_CONFIRMED_SIMILARITY=0.30`

`REALTIME_KEYFRAME_SEMANTIC_PROBE_FPS` 暂时作为 `REALTIME_SEMANTIC_INPUT_FPS` 的迁移 alias；新旧同时存在
且数值冲突时启动失败。alias 不早于 `0.3.0` 删除。

以下配置没有等价语义，实施后移除；若仍显式设置，启动时返回可解释迁移错误，禁止静默忽略：

- `REALTIME_KEYFRAME_STRUCTURAL_THRESHOLD`
- `REALTIME_KEYFRAME_COMBINED_THRESHOLD`

## 14. 失败与降级

- 解码失败：该帧拒绝 admission，返回现有安全协议错误。
- semantic queue 替换：正常过载行为，只增加 drop counter。
- image embedding 失败：不缓存、不写视觉记录；其他帧继续。
- VLM 失败：不生成 `VisualSemanticRecord`，保留此前最后成功记录。
- VLM 结果无效：与失败相同，不把 partial/raw 结果升级为事实。
- record text embedding 失败：当前 VLM 语义仍以 `index_status=unavailable` 写入同一 store 并可作为 latest
  snapshot，但不进入可检索子集；记录 `visual_semantic_index_failed`。
- store/evidence retention 失败：本次记录不发布到 live view 或历史查询，保留此前最后成功记录，不暴露
  虚假 history availability。
- query text embedding 失败：`visual_memory_search` 返回 `unavailable`。
- Mem0 失败：不影响任何实时或历史视觉能力。

## 15. 可观测性

新增或调整安全事件与计数：

- semantic input admitted/skipped/replaced；
- background/interactive queue wait 和 inference latency；
- image embedding success/failure/cache hit；
- semantic keyframe selected，reason 仅为 initial/semantic/max_interval/interactive；
- VLM success/failure；
- visual semantic record retained/evicted/index_failed；
- visual memory query status、候选数量和 latency。

事件不得记录原始图片、JPEG/base64、绝对路径、文本全文、向量、用户 query 或 Provider raw response。

## 16. 两种短期记忆方案比较

| 维度 | 全采样帧 image embedding 时间线 | 关键帧 VLM 语义时间线（本设计） |
| --- | --- | --- |
| 记录来源 | 每个成功语义入口帧 | 成功 VLM 理解的语义关键帧 |
| 保存内容 | image vector + evidence | 结构化 VLM 事实 + text vector + evidence |
| 查询 | text-to-image recall | text-to-text recall |
| 查询时 VLM | 需要二次复核 | 不需要 |
| 覆盖 | 较广 | 较窄但符合确认边界 |
| 延迟与成本 | 较高 | 较低 |
| 一致性 | 两次 VLM 可能不一致 | 实时与历史共用一次 VLM 结果 |
| 可解释性 | 向量只表示候选 | 直接返回 scene/object/action |
| 存储 | 更多 JPEG 和向量 | 仅保留成功理解记录 |
| Mem0 适配 | 不适合 | 仍不直接使用 Mem0 |

本设计选择关键帧 VLM 语义时间线，并明确接受未采样、未选中、被替换、VLM 失败或 VLM 未描述的视觉细节
不可查询。

## 17. 视觉上下文累积与压缩

每个新语义关键帧仍先由 SigLIP2 选出，再由 VLM 生成一条独立的结构化
`VisualSemanticRecord`。VLM 不负责检测关键帧。后续 VLM 调用可以读取同一 session、同一 video 时间线中
不晚于当前帧的视觉上下文，以建立人物、物体、场景和事件的连续性。

视觉上下文采用与 AgentRuntime conversation compaction 相同的预算心智模型：追加事实、计算完整请求预算、
保留最近原文、压缩较老连续前缀、重建请求并重新计数。复用通用 `ContextWindowPolicy` 及默认
`target=40%`、`trigger=70%`、`hard=85%` 比例，但视觉域拥有独立的模型输入上限、token counter、
safety margin、store 和 compactor。不得直接复用 conversation summary，也不得把视觉记录写入
`ConversationStore`。

每次后台 VLM 请求的输入按以下顺序编译：

```text
视觉观察指令
较早视觉前缀的结构化摘要（可选）
最近若干条未压缩 VisualSemanticRecord
当前关键帧图片
```

VLM 输出必须区分：

- `current_facts`：只由当前图片支持的场景、人物、物体、动作和画面文字；
- `changes`：当前图片相对历史上下文可确认的新增、消失、移动或状态变化；
- `uncertainties`：当前无法确认、被遮挡或与历史冲突的事实；
- 当前 `frame_sequence` 与 `captured_at_ms` 覆盖边界。

历史上下文只能辅助连续性判断，不能把上一帧事实自动继承为当前事实。当前图片不支持的对象必须标记为
未知、未观察到或变化候选，不能仅因历史文本存在就继续声称其当前存在。现有规范化记录可以继续保留
`scene/objects/people/actions/events/text_in_video/summary` 公共字段；上述 grounding 分区属于 VLM 输出校验和
上下文编译契约，不要求向主 LLM 暴露内部 prompt。

`VisualContextCompactor` 只压缩最老的连续前缀，并保留最近逐关键帧原文。结构化摘要至少包含覆盖的
sequence/time range、稳定场景、重要对象和人物的最后确认状态、已发生的状态变化、未解决的不确定性或冲突，
以及被覆盖的 record IDs。压缩成功后才能替换视觉上下文中的已覆盖前缀；压缩失败不得删除、改写或伪造
原始记录。

预算失败语义与 AgentRuntime 对齐：trigger 到 hard 之间压缩失败时保留原文并允许在预算内继续；达到 hard
后必须先压缩，重试仍不能收敛时跳过本次后台 VLM 观察并记录可解释错误，不向 Provider 发送超预算请求。
视觉 worker 继续使用 one-inflight、one-latest-pending，因此压缩期间允许丢弃中间后台帧，但不得形成积压，
也不得阻塞媒体 ACK 或主 Agent。

压缩只改变后续 VLM 的 context projection，不替代视觉事实源：

- `visual_memory_search` 继续以逐条 `VisualSemanticRecord` 的规范化文本 embedding 为主要检索对象；
- `live_view_inspect` 继续读取不晚于可信边界的最新成功记录，必要时才附带有界视觉时间线摘要；
- 压缩动作本身不删除 store 中的原始记录，记录淘汰仍只服从既有容量、TTL 和 session 生命周期；
- 第一阶段不为视觉摘要建立第二套搜索索引，也不把摘要写入 Mem0 或主 Agent prompt。

## 18. 测试与评测

临时 TDD 至少覆盖：

- 固定 5 FPS admission 不读取像素；
- semantic queue 保持一个 in-flight 和一个 latest pending，无积压；
- interactive job 替换 background pending 且不可被视频帧覆盖；
- 每个实际执行帧只产生一次共享 image embedding；
- Pixel Diff、SSIM 和结构/组合选帧不再参与运行路径；
- 关键帧只由 initial/semantic/max_interval/interactive 产生；
- 只有成功且有效的 VLM 结果形成视觉语义记录；
- live view 与历史查询读取同一 store；
- as-of 查询不返回未来记录；
- 查询不调用 VLM；
- session/user/TTL/runtime close 清理记录与 evidence；
- Mem0 不接收视觉记录，Mem0 unavailable 不影响视觉查询；
- 新 VLM 请求按 as-of 边界编译旧摘要、最近原文和当前图片；
- `current_facts` 不得无证据继承历史对象，`changes` 可以使用历史比较；
- 视觉预算复用通用 ratio 状态机，但使用独立模型上限、counter 和 safety margin；
- 只压缩最老连续前缀，成功前不替换覆盖记录；
- trigger 压缩失败可在预算内继续，hard 压缩失败跳过后台观察且保持 latest-wins 无积压；
- 压缩不改变逐记录历史检索、store retention、Mem0 或主 Agent prompt 边界。

真实 Provider system eval 通过显式 operator gate 验证 5 FPS 下的 embedding 吞吐、latest-wins 延迟、关键帧
召回、VLM 成功率和查询排序。Agent eval 保留“最后看到物体”和“未找到时诚实回答”两个 Task，并更新受控
环境为 VLM 语义记录查询。

## 19. 非目标

- 原始 30 FPS 全量 embedding；
- Pixel Diff、SSIM 或光流选帧；
- 查询时二次 VLM；
- 未成功理解画面的录像级搜索；
- 精确坐标、目标跟踪或跨 camera re-identification；
- 跨 session 自动视觉记忆；
- 自动把视觉事实 promotion 到 Mem0；
- 将视觉上下文与 conversation summary 合并或共用同一持久化记录；
- 用视觉压缩摘要替代逐关键帧语义事实或建立重复的第一阶段搜索索引；
- 新增 embedding、attention 或 memory 管理 Tool。
