# 统一多模态 Embedding 架构

Last updated: 2026-08-04

本文档是 `assistant_agent` 当前 image/text embedding 平台、session 短期视觉时间线和历史找物能力的
事实权威。媒体接入与关键帧生命周期见 `media-agent-service-websocket.md`，显式 Tool 治理见
`tool-calling-architecture.md`；源码和测试与本文冲突时，以源码和测试为准并回补本文。

## 产品与工具边界

本期新增的用户功能只有两个：

- 会话内短期视觉回忆：保留经过 semantic probe 的历史画面向量和 owned evidence，不要求它已被选为关键帧；
- 历史找物：用文本召回同 session 的历史画面，再由 VLM 对有界 top-k evidence 复核。

新增给主 LLM 的 Tool 只有 `visual_memory_search`。`live_view_inspect` 继续回答当前实时画面，内部
`realtime_video_observe` 继续生成 rolling VLM snapshot。`siglip2_embed*`、`find_object`、
`visual_attention_manage` 都不是注册 Tool。Attention 只产生内部候选，不发消息、不创建任务、不触发工具。

## 分层与数据流

```text
ImageObservation / TextObservation
        -> SessionEmbeddingCoordinator
        -> MultimodalEmbeddingProvider
        -> EmbeddingEvent | EmbeddingFailureEvent
        -> 独立有界 consumer queues
             ├─ KeyframeChangeConsumer
             ├─ TemporalMemoryConsumer
             ├─ CrossModalAlignmentConsumer
             ├─ VisualAttentionConsumer
             └─ VisualMemorySearchService（查询时读取时间线并复核）
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

## Semantic probe 与关键帧

`REALTIME_KEYFRAME_SEMANTIC_PROBE_FPS=2` 表示：当 SSIM 没有越过结构阈值时，每 0.5 秒至少安排一次
semantic probe。它不是 embedding 推理的 2 FPS 上限；首帧、SSIM 触发、强制最大间隔及其他消费者
需要时都可在两个保底 probe 之间产生共享推理。最终选帧保留 SSIM 与 semantic 各自触发、0.4/0.6
组合分数和最长 10 秒候选；像素差只用于采样与诊断。

语义关键帧不再自行创建视觉塔，而从 session coordinator 取得共享结果。Provider 失败时 semantic
score fail closed 为 0，SSIM 路径继续工作。

## 文本、ASR 与跨模态消费者

平台不直接处理语音。音频在上游转为稳定文本后，与键盘输入一样成为 `TextObservation`；`source`
只说明来源。Runtime 只对非空 final `request.text` 编码，且 session 必须存在 text consumer。文本
embedding 不写入 `TemporalVisualMemory` 或 Mem0。

Alignment 按 similarity 降序、时间距离升序关联同空间事件。Attention 只有设置内部 text target 后才
比较 image event 并保存 `visual_attention_candidate`；候选不会自动转为 Agent 行为。

## Session retention、as-of 与清理

`TemporalVisualMemory` 同时受记录数和总字节限制。Image consumer 只用同文件系统 hard-link 把 live
JPEG 变为 session-owned evidence；失败就记录 retention failure 且不索引，不复制媒体字节。淘汰、
session/user 删除、TTL eviction 和 runtime pool close 都删除向量记录与 owned link。普通 WebSocket
transport close 不清理，因此同 session 重连仍能复用历史。

查询时 Runtime 绑定 session，ToolContext 提供可信 as-of sequence；模型只能提交
`query/time_window/search_mode`。未来帧不能进入结果。Embedding 只召回，不输出坐标；VLM 只接收有界
top-k owned refs。VLM raw response 不进入结果，只投影复核后的 scene/objects。状态固定为
`confirmed|candidate|uncertain|not_found|unavailable`；复核失败保留候选，text embedding 失败返回 unavailable。

## Tool 暴露与安全

`visual_memory_search` 是 `category=read`、`requires_media=[]`，视频断线后仍可查询已有历史。Runtime
在 catalog 构建前删除调用方传入的 `_trusted_visual_memory_available`，再根据现有 coordinator 的
`has_history()` 覆盖；exposure 不检查请求关键词。执行仍经过
`ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。

向量、文本、owned evidence 路径不进入模型 schema、Tool data、trace、日志或 system eval artifact。
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
attention 管理 Tool，也不把 SigLIP2 当成完整 VLM。视觉塔做 semantic keyframe 只是统一 embedding
平台的一个消费者，不是平台本身。
