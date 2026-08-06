# Qdrant 视觉文本记忆检索设计

## 目标

把 `visual_memory_search` 从 SigLIP2 的 text-text 相似度检索迁移到本地 Qdrant 混合检索，解决明确物体词（例如“鼠标”）已出现在 VLM 文本中却未进入 Top-K 的 P0。主 LLM 仍只消费带时间戳的单帧 VLM 文本；SigLIP2 只负责图像相关能力。

## 数据流

1. 每个关键帧继续独立、并行调用 VLM。
2. 任一帧的 VLM 文本一旦完成，立即写入 session 本地时间线，并作为派生文档写入 Qdrant；不等待更早序号完成。
3. Qdrant point payload 保存 `user_id`、`session_id`、`video_id`、`record_id`、`frame_sequence`、`captured_at_ms` 和完整 VLM 文本。
4. `visual_memory_search` 使用严格的 user/session/时间/sequence filter，在 Qdrant 中同时执行 BM25 sparse 与 BGE dense 检索。
5. Qdrant 使用 Weighted RRF 融合，BM25:dense 权重为 `3:1`；两路各 prefetch 32 条，最终返回 12 条。
6. 工具把命中的 Qdrant payload 投影为带绝对时间和相对时间的文本列表，交给主 LLM。
7. 单帧写入以 `wait=false` 获得 WAL acknowledgment 后立即返回；查询以前以本地时间线最后一条
   `record_id` 做最多 250ms 的 point 可见性检查，超时则保留当前结果并标记 coverage 不完整。

## 模型与部署

- Dense 模型固定为 `BAAI/bge-small-zh-v1.5`，由 FastEmbed 在本地执行。
- Sparse 模型使用 Qdrant 服务端内置 BM25，配置 `language=none`、`tokenizer=multilingual`；FastEmbed
  BM25 不支持该 tokenizer，因此不得用于这一路。
- Qdrant 版本下限为 `1.17.0`，以使用原生 Weighted RRF。
- BGE 模型文件必须预置到本地缓存；服务运行时禁止联网下载。BM25 由 Qdrant 本地服务执行。
- Qdrant 是视觉文本的派生检索索引；session 内本地时间线仍是实时快照和证据生命周期的事实来源。

## 故障语义

- real mode 下 Qdrant、dense 模型或 sparse 模型不可用时，`visual_memory_search` 返回结构化 `unavailable`，并标记 `coverage_complete=false`。
- 禁止静默回退到 SigLIP2 text-text 检索。
- 单帧 VLM 文本仍写入本地时间线，因此实时画面快照不因检索服务故障而丢失。
- mock mode 使用显式的离线测试实现，不访问网络或真实 Provider。

## 生命周期与隔离

- 写入和查询都必须带 `user_id + session_id` 过滤，禁止跨用户或跨 session 召回。
- session/user 清理同时清理对应 Qdrant points；Qdrant 清理失败按派生索引故障记录，不阻止本地 session 生命周期完成。
- point ID 必须由稳定记录标识派生，使重复写入保持幂等。

## 验收

- 90 条事故文本中查询“鼠标”，Seq81-85 至少有一条进入结果，目标进入 Top 3。
- 结果最多 12 条，并保留帧时间戳和可读时间标签。
- 发布路径不再调用 SigLIP2 `embed_text`；SigLIP2 的关键帧选择和 image-text visual reminder 不受影响。
- Qdrant 不可用时返回结构化错误，且不存在 SigLIP2 text-text fallback。
