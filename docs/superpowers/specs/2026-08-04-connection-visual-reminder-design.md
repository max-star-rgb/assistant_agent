# 连接级视觉提醒设计

日期：2026-08-04

## 1. 背景与目标

当前实时视频链路对 H.264 独立帧完成协议校验和 FFmpeg 解码后，以固定 5 FPS 进入有界语义流水线。每个准入帧最多执行一次共享 SigLIP2 image embedding，再由纯语义关键帧选择器决定是否进入受治理的 VLM 视觉理解和 session 视觉语义存储。

本功能新增连接级视觉提醒。用户可以在当前 Agent-Service VIDEO WebSocket 连接中创建多条一次性提醒，例如“看到水烧开时提醒我”。系统只使用 SigLIP2 图文共同向量空间，把用户指定的视觉条件文本与每个已选关键帧的现有 image embedding 做匹配。命中后通过当前连接立即发送一次 `chatResponse`，不调用 VLM 复核，也不把提醒持久化到 Memory、durable task 或 proactive wake。

## 2. 范围

本期支持：

- 当前可信 Agent-Service VIDEO 连接内创建多条视觉提醒；
- 查看当前连接中的视觉提醒；
- 在命中前取消视觉提醒；
- 每条提醒首次命中后发送一次 `chatResponse` 并停止匹配；
- 直接复用已选关键帧的 SigLIP2 image embedding；
- 切换同一连接中的 `video_id` 时保留提醒；
- WebSocket 连接关闭时立即清空并注销全部提醒。

本期不支持：

- AUDIO 连接、规范化 `/ws/gateway`、HTTP、CLI 或其他入口的视觉提醒；
- 跨连接恢复、跨 session 搜索、长期保存或离线通知；
- 重复提醒、定时提醒、持久化通知重试；
- 使用 VLM、目标检测、OCR 或其他模型复核命中；
- 对固定 5 FPS 准入但未被选为关键帧的图像做提醒匹配；
- 用户或 LLM 为单条提醒修改匹配阈值。

## 3. 架构与组件边界

### 3.1 VisualReminderManager

新增连接级 `VisualReminderManager`，负责提醒状态和图文向量匹配。它只管理一条 Agent-Service WebSocket 连接对应的可信 `user_id + runtime_session_id`，不承担 WebSocket 编码、LLM 意图判断、VLM 调用或持久化职责。

每条提醒至少包含：

- `reminder_id`：连接内唯一的不透明标识；
- `target`：用于 SigLIP2 text embedding 的视觉条件；
- `message`：命中后发送给用户的提醒文案；
- `target_embedding`：经过共同空间校验的 text `EmbeddingEvent`；
- `created_at_ms`：创建时间；
- `status`：`pending | reserved | triggered | cancelled`。

Manager 使用锁保护创建、查看、取消、匹配、发送确认和关闭操作。原因是 Tool 可能在 runtime worker thread 中执行，而关键帧匹配与投递运行在 WebSocket event loop。

每条连接最多保存 16 条 `pending` 或 `reserved` 提醒。相同规范化 `target + message` 的重复创建返回现有 pending 提醒，不新增副本。已触发和已取消记录不参与去重或匹配，最多保留最近 64 条终态记录供当前连接查看；超出后按终态时间淘汰最旧记录。

### 3.2 受治理 Tool

新增 `visual_reminder_manage` Tool，支持以下 action：

- `create`：提交 `target` 和 `message`，计算一次 text embedding 并创建提醒；
- `list`：列出当前连接中的提醒及状态；
- `cancel`：按 `reminder_id` 取消尚未命中的提醒。

Tool 属于有状态写能力，完整经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。Tool 只能操作 runtime 依据可信请求身份解析出的连接级 Manager；模型不能提交或覆盖 `user_id`、`session_id`、manager 引用、embedding、阈值或连接对象。

Tool 只在以下结构化条件同时满足时暴露：

- 请求来自可信 Agent-Service entry profile；
- `assistantControl.callType=VIDEO`；
- 当前 runtime session 已注册活动的连接级 Manager。

Agent-Service 在成功处理 `assistantControl.callType=VIDEO` 后，以 `assistantControl.number + state.runtime_session_id` 创建并注册 Manager，因此用户不必等待第一帧视频到达即可创建提醒。注册失败时 VIDEO 握手失败关闭，不产生一个看似可用但无法管理提醒的连接。WebSocket 清理负责按同一身份注销 Manager。

暴露逻辑不得使用关键词、正则或手写话术判断用户意图。是否调用 Tool、选择哪个 action 以及如何填写 `target/message/reminder_id` 由 LLM 决定。

`list` 和 `cancel` 只返回提醒标识、视觉条件、提醒文案和状态，不返回向量、相似度历史、连接对象或内部 session 标识。

### 3.3 实时关键帧接入

`SemanticFramePipeline` 已通过选中回调把 `VideoFrame`、现有 image `EmbeddingEvent | None` 和选帧原因传给 `RealtimeVideoObserver`。当前 observer 丢弃该事件；本功能改为把非空事件交给连接级 Manager。

匹配发生在关键帧已选中之后、VLM 队列处理之前。提醒匹配和原有 VLM/视觉记忆流程相互隔离：提醒失败不能阻断 VLM，VLM 失败也不能撤销已经成功进入投递流程的提醒。

现有 `VisualAttentionConsumer` 保持实验性内部候选职责不变。它接收全部协调器 image/text event、只支持单 target，且不是连接级提醒的事实源。

### 3.4 Agent-Service 投递

命中提醒通过当前 Agent-Service WebSocket 的统一出站发送边界投递，消息类型为 `chatResponse`。主动通知使用独立 `chatIndex`：

```text
visual-reminder:<reminder_id>
```

正文使用提醒的 `message`。提醒响应与普通 chat turn、video ACK 和其他协议响应共用串行化发送边界，避免多个异步 producer 并发写 WebSocket 导致帧交错。

连接关闭时按以下顺序清理：先禁止 Manager 接收新建和新命中，再停止或排空连接拥有的发送工作，最后清空提醒并从 runtime session 注册表注销。切换 `video_id` 只关闭并重建 observer，不关闭连接级 Manager。

## 4. 数据流

### 4.1 创建提醒

```text
用户自然语言请求
  -> LLM 调用 visual_reminder_manage(action=create)
  -> runtime 注入可信连接级 Manager
  -> SessionEmbeddingCoordinator.embed_text(target)
  -> 校验 text embedding readiness、space、dimension、normalization 和向量有效性
  -> Manager 保存 pending 提醒
  -> Tool 返回 reminder_id、target、message、status
```

只对视觉条件 `target` 编码，不对包含“提醒我”等命令语义的完整通知文案编码。例如用户说“看到水烧开时提醒我”，模型应提交接近“水已经烧开”的 `target` 和适合直接通知的 `message`。

### 4.2 匹配与触发

```text
固定 5 FPS 准入帧
  -> 一次 SigLIP2 image embedding
  -> SemanticKeyframeSelector
  -> 已选关键帧及现有 EmbeddingEvent
  -> VisualReminderManager.match_keyframe(event)
  -> 与全部 pending target embedding 计算 cosine similarity
  -> 达到服务端阈值的提醒原子进入 reserved
  -> 当前连接加入 chatResponse 出站流程
  -> 入队成功：reserved -> triggered
  -> 原有 VLM 和视觉语义存储继续处理
```

匹配阈值由服务端配置，默认 `0.82`。Manager 使用统一 `EmbeddingComparator`，只有 embedding space、dimension、normalization、有限值和非零 norm 全部兼容时才计算 cosine。

一个关键帧可以同时命中多条提醒；每条提醒独立预留和投递。每条提醒一旦进入 `triggered` 就永久退出匹配集合。固定准入但未选中的帧不匹配。Provider 失败后因 interactive 或 max interval 降级选出的关键帧，其 `EmbeddingEvent` 为空，因此不匹配提醒，但仍可进入原有 VLM 流程。

### 4.3 查看与取消

`list` 返回当前连接的有界提醒快照。`cancel` 只能取消 `pending` 状态；不存在、已预留、已触发或已取消时返回稳定结构化状态，不把竞争结果伪装成成功取消。

自然语言查看或取消仍由 LLM 选择并调用 Tool。系统不依据用户文本直接操作提醒。

## 5. 并发、失败与一次性语义

- `match_keyframe` 在锁内对命中提醒执行 `pending -> reserved`，相邻关键帧不能重复预留同一提醒。
- `cancel` 与命中竞争时只有先获得锁的一方能改变 `pending`；最终只能是 `cancelled` 或进入投递流程，不能同时成立。
- `chatResponse` 成功加入当前连接的出站流程后执行 `reserved -> triggered`。
- 若入队失败但连接仍活动，则执行 `reserved -> pending`，允许后续关键帧重新触发；当前关键帧不做同步重试。
- 若连接已进入关闭流程，入队失败后直接清理，不恢复、不持久化、不转入通用通知 outbox。
- text embedding 失败、text readiness 不可用或结果非法时，`create` 返回结构化 unavailable/failed 结果且不创建提醒。
- 单条提醒发生 comparison error 时跳过该提醒并记录清理后的内部观测，不影响同关键帧上的其他提醒。
- Manager 或投递回调异常不得阻断关键帧进入原有 VLM 队列。
- 本功能不调用 VLM 验证 SigLIP2 命中，因此提醒代表“视觉语义相似度达到配置阈值”，不升级为经 VLM 确认的事实。

## 6. 安全与观测

- reminder manager 的 identity 和 lifecycle 均由 runtime/Agent-Service 注入，调用方和模型不能指定其他 owner/session。
- 不在 Tool schema、Tool result、模型上下文、日志、trace 或协议响应中暴露向量。
- 常规日志和 trace 不记录完整 `target` 或 `message`，只记录提醒 ID 摘要、活动数量、状态、是否达到阈值、错误码和有界延迟。
- 不保存关键帧路径作为提醒证据，不新增 evidence retention；原有 VLM/视觉语义存储继续独立拥有其证据生命周期。
- `chatResponse` 只发送用户创建时确定的提醒文案，不把模型、Provider 或异常的原始 payload 投递给媒体端。

## 7. 测试与验收

首版测试放入独立 `tests/tdd/<feature>/` 临时 RED/GREEN 目录，是否晋升永久 core 测试由后续 core invariant 评审决定。

最小测试范围包括：

- Manager：多提醒、重复创建去重、16 条上限、查看、取消、关闭清理；
- 匹配：阈值上下边界、共同空间不兼容、非法向量、单条失败隔离、一个关键帧同时命中多条提醒；
- 并发：取消与命中竞争只产生一个确定终态；相邻关键帧不重复预留或通知；
- 流水线：只有已选关键帧匹配，未选帧不匹配，embedding failure 降级帧不匹配；
- 向量复用：统计 image provider 调用次数，证明提醒功能没有增加 image embedding；
- Agent-Service：VIDEO 连接可创建、查看和取消；命中发送独立 `chatResponse`；普通 chat 与提醒响应不交错；切换 `video_id` 保留提醒；断开后 Manager 被清空和注销；
- 生命周期失败：text embedding 不可用、投递入队失败恢复、连接关闭期间不恢复。

全部 pytest 默认运行在 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 SigLIP2、VLM、联网 Provider 或外部服务。

验收不变量：

1. 每个准入帧仍最多执行一次 image embedding，提醒匹配复用最终关键帧的现有向量。
2. 只有最终已选关键帧参与提醒匹配。
3. 提醒路径不调用 VLM。
4. 当前连接内可同时存在多条提醒，每条最多通知一次。
5. 提醒响应通过当前连接的串行化 `chatResponse` 边界发送。
6. WebSocket 断开后提醒状态不可恢复且不会转入持久化通知。

## 8. 文档同步

实现完成后同步维护：

- `docs/multimodal-embedding-architecture.md`：增加视觉提醒的数据流、共同空间复用和非目标调整；
- `docs/media-agent-service-websocket.md`：增加主动视觉提醒 `chatResponse` 的连接级语义、`chatIndex` 和并发发送约束；
- `docs/tool-calling-architecture.md`：记录 `visual_reminder_manage` 的 Tool 治理、结构化 exposure 和 runtime-owned manager 绑定。
