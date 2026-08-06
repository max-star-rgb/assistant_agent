# 关键帧并行 VLM 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每个已选关键帧立即进行相互隔离的并行 VLM 推理，chat 精确等待 A 时刻前最近已选关键帧，不受更早未完成帧阻塞。

**Architecture:** 保留单线程、有界的 SigLIP2 选帧流水线；仅把选帧之后的 VLM worker 从 one-inflight/one-pending 改为每关键帧一个 asyncio task。生产环境为每个 task 创建独立 ToolRegistry/VLM adapter，避免共享 Qwen WebSocket 状态。chat 到达时先从 observer 的已选/处理中/已完成关键帧中冻结不晚于 A 的最大 sequence；只有尚无关键帧时才交互式提升原始 A 帧。

**Tech Stack:** Python 3.12、asyncio、同步 Provider adapter（通过 `asyncio.to_thread`）、Pydantic、pytest。

## Global Constraints

- 不改变 H.264、video ACK、Tool 治理链与 mock/real Provider 门禁。
- 不等待更早 sequence；目标 sequence 成功后立即唤醒 `live_view_inspect`。
- 不共享有状态 Qwen realtime adapter；每个并发关键帧拥有独立 Provider conversation。
- 不新增并发上限或重试策略；Provider 失败按单帧独立失败处理。
- 不修改 core invariant；测试只放在临时 `tests/tdd/parallel-keyframe-vlm/`。

---

### Task 1: 固定并发与乱序完成契约

**Files:**
- Create: `tests/tdd/parallel-keyframe-vlm/test_parallel_keyframe_vlm.py`

**Interfaces:**
- Consumes: `RealtimeVideoObserver.promote()`、`SessionVisualSemanticStore.wait_for_sequence()`。
- Produces: 两个关键帧的 Provider 调用可以同时开始，sequence 2 可在 sequence 1 未完成时发布。

- [ ] **Step 1: 写失败测试**：使用可独立阻塞 sequence 1/2 的 adapter factory；断言 sequence 2 在 sequence 1 释放前开始并写入 Store。
- [ ] **Step 2: 运行 RED**：显式运行该 feature，预期旧串行 worker 无法启动 sequence 2。
- [ ] **Step 3: 最小实现**：把 observer VLM 队列替换为每关键帧 task，并传入 per-call registry factory。
- [ ] **Step 4: 运行 GREEN**：断言乱序发布和资源清理通过。

### Task 2: chat 冻结最近已选关键帧

**Files:**
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Test: `tests/tdd/parallel-keyframe-vlm/test_parallel_keyframe_vlm.py`

**Interfaces:**
- Consumes: `latest_keyframe_at_or_before(video_id, target_sequence)`。
- Produces: `PreparedChat.video_target_frame` 是 A 之前 sequence 最大的已选关键帧。

- [ ] **Step 1: 写失败测试**：已选 sequence 2、最新原始 sequence 3 时，保护阶段必须冻结 2。
- [ ] **Step 2: 运行 RED**：预期旧逻辑仍冻结并提升 3。
- [ ] **Step 3: 最小实现**：优先选择已选关键帧；仅无候选时提升原始帧；避免 delivery 阶段重复 promote。
- [ ] **Step 4: 运行 GREEN**：验证 exact sequence、pin/release 与未来帧隔离。

### Task 3: 生产实例隔离、文档与回归验证

**Files:**
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `.superpowers/brainstorm/970960-1785913562/content/live-view-full-flow.html`

**Interfaces:**
- Consumes: `create_realtime_video_observation_registry(config, ...)`。
- Produces: 每任务独立 registry/client/adapter；后台并发语义与文档一致。

- [ ] **Step 1: 生产 factory 改为传递 registry factory**，每个关键帧调用新建 Tool 与 Qwen adapter。
- [ ] **Step 2: 更新权威文档与浏览器伴侣**，删除 one-inflight/one-pending VLM 描述。
- [ ] **Step 3: 运行 feature、相关视觉专项、Ruff、compileall 与 `git diff --check`**。

