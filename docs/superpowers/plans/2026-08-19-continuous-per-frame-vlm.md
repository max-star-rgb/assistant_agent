# 实时视频逐帧 VLM 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每个成功解码的实时视频帧立即启动独立 VLM，并让 chat 只冻结和消费逐帧文本边界，同时完整保留语义关键帧选择与视觉提醒支路。

**Architecture:** `RealtimeVideoObserver.submit()` 将同一帧分发到两个互不互相 gating 的后台支路：逐帧 VLM 支路立即复制证据并创建 frame-owned service/client/WebSocket task；semantic 支路继续使用固定频率、embedding、latest-wins 和 `SemanticKeyframeSelector`，但 selected callback 只发布视觉提醒并释放其临时证据，不再调用 VLM。chat 到达时 `VisualPerceptionSession.prepare_strict_window()` 只冻结最近五帧的 sequence metadata，不 promotion、不补跑 VLM；`live_view_inspect` 继续等待 exact target 并读取 ready subset。

**Tech Stack:** Python 3.12、asyncio、LangGraph native ToolRuntime、Pydantic、pytest。

**Spec:** 用户在 2026-08-19 对实时视觉语义的明确修正：每帧一个独立 VLM；第 8 帧完成立即返回，不等待第 7 帧；语义关键帧算法保留为独立支路。

## Global Constraints

- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，pytest 不调用真实 Provider。
- 每个 frame sequence 最多一次 VLM；semantic selected callback 不得造成重复 VLM。
- 每帧独立 service/client/adapter/WebSocket，禁止共享 client 或 Provider 限流时静默回退。
- semantic sampler、image embedding、selector、latest-wins 和视觉提醒契约保持不变。
- chat 只冻结 window metadata；exact target 最多等待 4 秒，target 完成即返回 ready subset。
- 不修改 `tests/core`；临时 RED/GREEN 继续放在 `tests/tdd/realtime-visual-target-window/`。
- 保留工作区中与本功能无关的用户改动。

---

### Task 1: 用 RED 锁定逐帧 VLM 与关键帧支路并存

**Files:**
- Modify: `tests/tdd/realtime-visual-target-window/test_observer_window.py`
- Modify: `tests/tdd/realtime-visual-target-window/test_target_window.py`

**Interfaces:**
- Consumes: `RealtimeVideoObserver.submit(frame)`、`VisualPerceptionSession.prepare_strict_window(video_ids)`。
- Produces: 可观察契约——连续 submit 4–8 会启动五个并行 observation；semantic selector 仍收到 embedding；chat freeze 不调用 `promote_window()`。

- [ ] **Step 1: 写逐帧 VLM RED**

新增测试连续 `await observer.submit(frame)` 4–8，在不调用 `promote_window()` 的情况下等待五个 observation 全部进入，并断言 sequence 集合为 `{4,5,6,7,8}`、`max_active == 5`。

- [ ] **Step 2: 写 semantic 分支无重复 VLM RED**

使用记录 image embedding/selected callback 的真实 `SemanticFramePipeline` 组合，断言 selector 仍处理 admitted frame，同时 selected frame 不触发第二次相同 sequence 的 observation。

- [ ] **Step 3: 写 chat 纯冻结 RED**

将 recording observer 的 `promote_window()` 改为失败即抛异常；调用 `prepare_strict_window()` 仍应返回 `(4,5,6,7,8)`，证明 chat 不触发 VLM。

- [ ] **Step 4: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window/test_observer_window.py \
  tests/tdd/realtime-visual-target-window/test_target_window.py
```

Expected: 至少逐帧 VLM 与纯冻结测试失败，原因分别是 `submit()` 仍受 semantic selector gating、`prepare_strict_window()` 仍调用 promotion。

### Task 2: 分离逐帧 VLM 与 semantic keyframe 支路

**Files:**
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/media/visual_perception/module.py`

**Interfaces:**
- Produces: `submit(frame)` 对每个新 sequence 立即 `_enqueue()` 一次，同时异步提交原 semantic pipeline。
- Produces: semantic selected callback 只执行 reminder publication 和 transferred evidence cleanup。
- Changes: `prepare_strict_window()` 只返回 `VisualTargetWindow`，不调用 observer promotion。

- [ ] **Step 1: 实现 submit 双支路**

`_submit()` 先以 `window_role="background"` 调用 `_enqueue()` 创建逐帧 VLM task，再把同一原始 frame 提交给现有 `SemanticFramePipeline`；两条支路各自保留证据，VLM 不等待 embedding 结果。

- [ ] **Step 2: 去除 semantic selected 到 VLM 的重复调用**

将 `_enqueue_semantic_selection()` 收敛为 reminder callback：保留 `visual_reminder_registry.publish_image_event(...)`，随后删除 semantic pipeline 转交的临时文件。不得调用 `_enqueue()`。

- [ ] **Step 3: 把 chat 改为纯 metadata freeze**

删除 `prepare_strict_window()` 中的 `observer.promote_window(...)`；继续验证 window sequence 严格递增并生成可信 `window_id/start/target`。

- [ ] **Step 4: 运行 GREEN**

重复 Task 1 命令，Expected: PASS。

- [ ] **Step 5: 提交行为修正**

只 stage 本任务源码和测试，提交信息：`fix: start one realtime VLM per decoded frame`。

### Task 3: 修正观测、system eval 与权威文档

**Files:**
- Modify: `tests/tdd/realtime-visual-target-window/test_window_observability.py`
- Modify: `evals/system/realtime_visual_target_window/runner.py`
- Modify: `evals/system/realtime_visual_target_window/README.md`
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `scripts/README.md`

**Interfaces:**
- VLM span 在 frame arrival 时产生，只带 sequence 和 isolated-client 事实，不伪造尚不存在的 chat window role。
- target barrier span 在 Tool 调用时关联 `window_id/start/target` 和 ready/missing counts。
- system eval 先 submit 五帧，再执行 Tool；不得通过 chat promotion 启动 Provider 调用。

- [ ] **Step 1: 写 observability RED**

断言逐帧 submit 产生五个 `vlm.infer.finished`，每个带 `frame_sequence` 与 `provider_connection_isolated=true`；frame span 不要求 chat window ID。

- [ ] **Step 2: 更新 system eval**

runner 对五帧逐一 `observer.submit()`，等待所有 observation 已启动后执行 exact-target Tool。输出继续保持 content-free。

- [ ] **Step 3: 更新 authority**

明确 VLM 主路径是 every decoded frame；semantic keyframe algorithm 保留但只服务 embedding/selector/reminder 派生能力；chat freeze 不触发 VLM。

- [ ] **Step 4: 运行专项 GREEN 和 dry-run**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_realtime_visual_target_window_eval.py --dry-run
```

- [ ] **Step 5: 提交观测与文档**

选择性 stage 本任务 hunks，提交信息：`docs: define continuous per-frame realtime vision`。

### Task 4: 完成离线、authority 与 8089 验证

**Files:**
- Verify only.

**Interfaces:**
- Produces: 新鲜测试、静态检查和现有 8089 hot reload 证据。

- [ ] **Step 1: 运行最小专项测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window
```

- [ ] **Step 2: 运行共享基础设施回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

- [ ] **Step 3: 运行 authority、compile、ruff 和 diff 检查**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent/media src/assistant_agent/agent_server
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check src/assistant_agent/media evals/system/realtime_visual_target_window
git diff --check
```

- [ ] **Step 4: 验证唯一 8089 server reload**

只检查现有 `127.0.0.1:8089` 进程、最新 reload 日志和 `/health/agent-server-adapter`；不得启动第二个 server。

- [ ] **Step 5: 汇报**

```text
Core invariant: unchanged.
Tests: updated tests/tdd/realtime-visual-target-window for temporary RED/GREEN; user may delete the directory manually.
```

若未获真实 Provider 授权，明确 system eval real run 未执行。
