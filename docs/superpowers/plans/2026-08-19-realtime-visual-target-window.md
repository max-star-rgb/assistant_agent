# Realtime Visual Target Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在原生 LangGraph 生产链中实现“提问时冻结最近五帧、每帧独立并行 VLM、只以最新目标帧解除前台屏障”的实时视觉语义。

**Architecture:** Media 入口从共享 raw frame store 冻结一个可信 `VisualTargetWindow`，observer 通过绕过 latest-wins selection queue 的 strict enqueue 接管五帧副本。每个 sequence 使用独立 observation service/client/Qwen WebSocket；live-view Tool 只等待 exact target，并只投影冻结 sequence 范围内当时已完成的记录。Agent Server、`AssistantRootGraph`、fast `create_agent` 和标准 ToolNode 边界保持不变。

**Tech Stack:** Python 3.11、asyncio、blocking Provider WebSocket adapter、LangGraph/LangChain ToolNode、Pydantic、pytest（mock/offline）、LangSmith native tracing。

**Spec:** `docs/superpowers/specs/2026-08-19-realtime-visual-target-window-design.md`

## Global Constraints

- 不恢复旧 Gateway/Runtime facade，不把主 Agent loop 放回 Media route。
- strict window 固定为最近五个成功解码帧；不足五帧时使用全部可用帧。
- target 是窗口最大 sequence；前台只等待 exact target，最多 4.0 秒。
- target 成功后不得等待 context 帧或 observer idle；target 失败/超时不得用旧帧伪装当前画面。
- 每个 VLM task 必须拥有独立 service/client/adapter/WebSocket，不共享 `QwenRealtimeVisionAdapter` 可变状态。
- Graph state 和 trace 不保存 JPEG、路径、VLM 正文或 Provider 原始响应。
- 默认 mock/offline；真实 Provider 验证需 `MULTIMODAL_AGENT_PROVIDER_MODE=real`、完整配置、runner 显式开关与 operator 确认。
- 当前工作区已有用户改动；实现和提交时只 stage 本计划列出的相关文件，不覆盖或顺带提交其他 diff。
- Core invariant: unchanged。该变更属于 realtime visual feature，不修改 `tests/core`；RED/GREEN 只放 `tests/tdd/realtime-visual-target-window/`，用户可在完成后手动整目录删除。

---

### Task 1: 建立五帧冻结窗口契约

**Files:**
- Modify: `src/assistant_agent/media/video/video_context.py`
- Modify: `src/assistant_agent/media/video/h264_video_ingestion.py`
- Modify: `src/assistant_agent/media/visual_perception/module.py`
- Create: `tests/tdd/realtime-visual-target-window/test_target_window.py`

**Interfaces:**
- Produces: `REALTIME_VISUAL_TARGET_WINDOW_SIZE = 5`
- Produces: `VisualTargetWindow(window_id, video_id, start_sequence, target_sequence, sequences)`
- Produces: `VisualPerceptionSession.prepare_strict_window(video_ids) -> VisualTargetWindow | None`
- Preserves: frame payloads remain outside Graph state.

- [ ] **Step 1: 写冻结窗口 RED 测试**

使用 `InMemoryVideoContextStore(window_size=5)` 和 recording observer，依次写入 sequence 1–8。断言 chat 时只冻结 4–8，`promote_window()` 一次收到不可变的五帧副本；只有 1–3 帧时窗口自然缩短。

```python
window = await session.prepare_strict_window([video_id])
assert window.start_sequence == 4
assert window.target_sequence == 8
assert window.sequences == (4, 5, 6, 7, 8)
assert observer.promoted_sequences == [(4, 5, 6, 7, 8)]
```

另测多 video ID 时只选择请求顺序中最后一个有帧的可信 video，且 frame list 必须同 video、严格递增。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window/test_target_window.py
```

Expected: FAIL，因为当前只有 `prepare_strict_target()` 和单帧 `_latest_frames`。

- [ ] **Step 3: 实现统一窗口常量与 store-backed snapshot**

把 raw context 和 H.264 ingestion 默认 window 从各自的 `3` 统一到 `REALTIME_VISUAL_TARGET_WINDOW_SIZE`。`VisualPerceptionSession` 注入 `VideoContextStore`，不再维护第二份 `_latest_frames` 真相；`prepare_strict_window()` 调用：

```python
frames = store.get_recent_frames(video_id, limit=REALTIME_VISUAL_TARGET_WINDOW_SIZE)
await observer.promote_window(frames)
return VisualTargetWindow(
    window_id=f"visual-window-{uuid4().hex}",
    video_id=video_id,
    start_sequence=frames[0].sequence,
    target_sequence=frames[-1].sequence,
    sequences=tuple(frame.sequence for frame in frames),
)
```

`promote_window()` 内部先把 target 交给 observer、再投递 context 帧；输入和返回契约中的 `sequences` 都保持时间升序。不要让 `prepare_strict_window()` 等待 VLM。

- [ ] **Step 4: 运行 GREEN 与 raw retention 回归**

运行 Task 1 测试，并显式覆盖 H.264 sequence 8 时 4–8 文件仍存在、1–3 已淘汰。

- [ ] **Step 5: 提交 Task 1**

```bash
git add src/assistant_agent/media/video/video_context.py \
  src/assistant_agent/media/video/h264_video_ingestion.py \
  src/assistant_agent/media/visual_perception/module.py \
  tests/tdd/realtime-visual-target-window/test_target_window.py
git commit -m "feat: freeze realtime visual target windows"
```

---

### Task 2: 实现 strict window 同序号 single-flight 与并行 enqueue

**Files:**
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Create: `tests/tdd/realtime-visual-target-window/test_observer_window.py`

**Interfaces:**
- Produces: `RealtimeVideoObserver.promote_window(frames: Sequence[VideoFrame]) -> WindowPromotionResult`
- Produces: sequence reservation covering semantic record, queued copy and active observation task.
- Preserves: `submit()` still uses adaptive semantic selection for background history.

- [ ] **Step 1: 写并行和去重 RED 测试**

用 blocking fake observation service factory 记录 active sequence：调用 `promote_window(4..8)` 后，等待五个 fake service 都进入 `observe()`，断言 `max_active == 5`。

增加竞态用例：让 background `on_selected(sequence=8, already_retained=True)` 与 strict promotion 同时到达，断言：

- sequence 8 只有一个 observation task；
- factory 只创建一次；
- 重复 retained 文件被删除；
- `WindowPromotionResult.reused_sequences == (8,)`。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window/test_observer_window.py
```

Expected: FAIL，因为没有 `promote_window()`，且当前 promotion 经过单 pending semantic pipeline。

- [ ] **Step 3: 增加 sequence reservation**

在 `_enqueue_lock` 保护下维护 `_reserved_sequences`。`_sequence_is_represented()` 必须检查：

```python
return (
    sequence in self._reserved_sequences
    or sequence in self._observation_tasks
    or semantic_store.has_exact_sequence(video_id, sequence=sequence)
)
```

复制前注册 reservation；任务登记成功后转入 `_observation_tasks`；复制/建任务失败时在 `finally` 清 reservation。不要用 `at_or_before()` 推断 exact identity。

- [ ] **Step 4: 实现绕过 latest-wins queue 的窗口 promotion**

`promote_window()` 只调用 observer 自己的 `_enqueue()`，不调用 `semantic_pipeline.promote()`。先 enqueue target，再用 `asyncio.gather()` enqueue 其余四帧；enqueue 完成只表示文件和 task 已被 observer 接管。

修复 `already_retained=True` 的两个 duplicate early-return：既然 semantic pipeline 已把文件 ownership 转给 callback，observer 必须在返回 `False` 前删除该副本。

- [ ] **Step 5: 保持取消与 close ownership**

窗口 promotion 使用 observer-owned task + `asyncio.shield()`，沿用 `3025e523` 的原则：chat 超时/取消不能留下半复制文件，也不能取消已经接管的背景 VLM。close 必须结算 reservation、observation task 和未发布文件。

- [ ] **Step 6: 运行 GREEN 与泄漏检查**

运行两个 TDD 文件；测试结束后断言临时 keyframe 目录为空或只剩 semantic store 正式 evidence。

- [ ] **Step 7: 提交 Task 2**

```bash
git add src/assistant_agent/media/video/realtime_video_observer.py \
  tests/tdd/realtime-visual-target-window/test_observer_window.py
git commit -m "fix: enqueue strict visual windows independently"
```

---

### Task 3: 每帧创建独立 VLM client/adapter/WebSocket

**Files:**
- Modify: `src/assistant_agent/media/visual_perception/observation_service.py`
- Modify: `src/assistant_agent/media/visual_perception/module.py`
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/media/video/video_adapter.py`
- Create: `tests/tdd/realtime-visual-target-window/test_isolated_vlm_clients.py`

**Interfaces:**
- Produces: `RealtimeVisualObservationServiceFactory`
- Changes: observer constructor accepts `observation_service_factory`, not one shared service.
- Guarantees: one factory product and one close per newly executed sequence.

- [ ] **Step 1: 写隔离 RED 测试**

factory 每次返回带唯一 client ID 的 service；五帧并发时断言：

```python
assert created_client_ids == {"client-4", "client-5", "client-6", "client-7", "client-8"}
assert all(service.close_count == 1 for service in services)
assert len({outcome.diagnostics["target_sequence"] for outcome in outcomes}) == 5
```

增加异常用例，证明 sequence 7 client 创建/observe 失败不会关闭或污染 4、5、6、8。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window/test_isolated_vlm_clients.py
```

Expected: FAIL，因为 observer 当前共享一个 service/client。

- [ ] **Step 3: 把 service 生命周期收进单帧调用**

新增同步 helper `_observe_with_isolated_service(request, trace_context)`，在同一 worker thread 中 create/observe/close。`_run_observation()` 只对该 helper 调用 `asyncio.to_thread()`，不得在 event loop 上执行 blocking WebSocket。

- [ ] **Step 4: 修正 realtime adapter factory**

`create_realtime_video_understanding_adapter()` 不再为背景 realtime client 设置 `close_connection_on_return=False`。每个 frame-owned service 只执行一次观察并关闭；uploaded media 的 process-owned client 生命周期不变，避免误伤 `77ce7c9a` 的上传媒体优化。

- [ ] **Step 5: observer close 不再关闭共享 service**

删除 `self.observation_service.close()` 路径；close 只等待/取消 frame-owned tasks。测试确保 close 与 frame finally 并发时每个 service 仍只关闭一次。

- [ ] **Step 6: 运行 GREEN**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window/test_observer_window.py \
  tests/tdd/realtime-visual-target-window/test_isolated_vlm_clients.py
```

- [ ] **Step 7: 提交 Task 3**

```bash
git add src/assistant_agent/media/visual_perception/observation_service.py \
  src/assistant_agent/media/visual_perception/module.py \
  src/assistant_agent/media/video/realtime_video_observer.py \
  src/assistant_agent/media/video/video_adapter.py \
  tests/tdd/realtime-visual-target-window/test_isolated_vlm_clients.py
git commit -m "fix: isolate realtime VLM connections per frame"
```

---

### Task 4: 将冻结窗口边界投影进原生 Graph 和 ToolRuntime

**Files:**
- Modify: `src/assistant_agent/agent_server/media_app.py`
- Modify: `src/assistant_agent/media/runtime_media.py`
- Modify: `src/assistant_agent/tools/runtime.py`
- Create: `tests/tdd/realtime-visual-target-window/test_native_window_projection.py`

**Interfaces:**
- Produces live-camera block fields: `window_id`, `window_start_sequence`, `target_sequence`.
- Produces: `RuntimeMediaSnapshot.visual_window_id` and `visual_window_start_sequence`.
- Produces ToolContext metadata: `visual_window_id`, `visual_window_start_sequence`, `visual_target_sequence`.
- Preserves: `AssistantRootInput` remains standard messages + structured `execution_mode`.

- [ ] **Step 1: 写 projection RED 测试**

从 `media_graph_input()` 构造 live-camera block，断言 `latest_runtime_media()` 和 `tool_context()` 只接受由 `source=live_camera` 投影的非负整数边界。uploaded video、普通 text 或 malformed/bool 字段不能获得 strict window metadata。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window/test_native_window_projection.py
```

- [ ] **Step 3: 替换 Media 单 target 调用**

chat handler 调用 `prepare_strict_window()`，把 start/target 传入 `_run_chat()` 和 `media_graph_input()`。不要传 frame path、window task 或 observer 对象。

- [ ] **Step 4: 增加可信字段解析**

`RuntimeMediaSnapshot` 同时保存 window ID 和 start/target；window ID 必须是受限长度的非空字符串，且只有 `0 <= start <= target` 时才投影完整窗口，否则 fail closed 为无 strict window。`latest_human_request()` 和 `tool_context()` 使用同一校验逻辑，避免两个 parser 漂移；必要时抽取一个窄 helper。

- [ ] **Step 5: 运行 GREEN，并验证 fast route 不变**

断言 `execution_mode` 仍为 `fast`，root graph 节点和 public input schema 无新增平行状态。

- [ ] **Step 6: 提交 Task 4**

```bash
git add src/assistant_agent/agent_server/media_app.py \
  src/assistant_agent/media/runtime_media.py \
  src/assistant_agent/tools/runtime.py \
  tests/tdd/realtime-visual-target-window/test_native_window_projection.py
git commit -m "feat: project trusted visual window boundaries"
```

---

### Task 5: exact-target barrier 与 ready-subset 窗口读取

**Files:**
- Modify: `src/assistant_agent/media/video/semantic_store.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py`
- Create: `tests/tdd/realtime-visual-target-window/test_target_barrier.py`

**Interfaces:**
- Produces: `SessionVisualSemanticStore.records_in_sequence_range(...)`.
- Changes: live-view wait deadline from 10 秒收敛为 `REALTIME_VISUAL_TARGET_WAIT_SECONDS = 4.0`.
- Produces structured result fields: `window_start_sequence`, `target_sequence`, `ready_sequences`, `missing_sequences`, `target_ready`.

- [ ] **Step 1: 写第 7 帧阻塞、第 8 帧先完成的 RED 测试**

启动 Tool 调用，发布 4、5、6，保持 7 pending，再发布 8。记录完成时钟并断言 Tool 在 8 发布后一个 event-loop turn 内返回：

```python
assert result.success is True
assert result.data["ready_sequences"] == [4, 5, 6, 8]
assert result.data["missing_sequences"] == [7]
assert result.data["target_ready"] is True
assert returned_before_frame_7_release is True
```

- [ ] **Step 2: 写未来帧和旧帧隔离 RED 测试**

store 中预置 sequence 3 和 9，目标窗口为 4–8；断言结果既没有 3，也没有 9。随后完成 7，断言已经返回的 result 不变。

- [ ] **Step 3: 写 target failure/timeout RED 测试**

将 deadline patch 为 0.01 秒；target 8 失败或超时时，即使 sequence 7 成功，也必须返回 `usable_visual_text=false` 和 exact target 状态，不得返回 sequence 7 summary。

- [ ] **Step 4: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window/test_target_barrier.py
```

- [ ] **Step 5: 实现 exact range API**

`records_in_sequence_range()` 在 store lock 内按 `(frame_sequence, created_at_ms)` 排序并 deep-copy；校验非负且 `start <= end`。不要把现有“最近 N 条”接口改成隐式窗口语义，历史检索仍可保留原 API。

- [ ] **Step 6: 重写 live-view projection**

Tool 先调用现有 `wait_for_sequence(exact=target)`；只有 semantic store 中 exact target 成功时才读取 `[start, target]` ready records。输出把 target observation 与 context observations 分开或明确标记 role，确保最终模型把第 8 帧当当前事实。

失败、timeout、store close 均走 `_memory_unavailable_result()`，但不得把 `at_or_before(target)` 的旧 record 投影成成功 snapshot。

- [ ] **Step 7: 运行 GREEN**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window
```

- [ ] **Step 8: 提交 Task 5**

```bash
git add src/assistant_agent/media/video/semantic_store.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py \
  tests/tdd/realtime-visual-target-window/test_target_barrier.py
git commit -m "fix: release live view on exact target completion"
```

---

### Task 6: 增加逐帧与 barrier 可观测性

**Files:**
- Modify: `src/assistant_agent/media/vision/observability.py`
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py`
- Modify: `docs/observability-harness.md`
- Create: `tests/tdd/realtime-visual-target-window/test_window_observability.py`

**Interfaces:**
- Produces safe attributes: window ID、sequence boundaries、role、isolation/reuse、wait/status/counts.
- Preserves: canonical `vlm.infer` generation and LangSmith native Graph tree.

- [ ] **Step 1: 写安全 trace RED 测试**

使用 in-memory trace store 运行 4–8，断言五个 `vlm.infer` generation 有不同 span ID 和 frame sequence，target role 只有 sequence 8。barrier finished 只含数字、布尔、枚举和随机 window ID。

递归断言 trace 不含 frame path、JPEG/Base64、用户文本、VLM summary 或 Provider raw payload。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window/test_window_observability.py
```

- [ ] **Step 3: 实现关联字段**

`VisualTargetWindow` 创建随机 `window_id`，只在 media block/runtime metadata/trace 中传播，不进入用户可见文本。逐帧 trace 使用现有 `observe_vision_inference()`，只扩展 allowlist 字段，不新建 shadow tree。

- [ ] **Step 4: 记录 barrier 终态**

started/finished 事件记录 target 状态和 wait latency。第 7 帧晚完成应出现在自己的 VLM trace，不能延长 barrier span。

- [ ] **Step 5: 文档说明 `fast` trace 渲染**

在 observability authority 中说明 `route_execution_mode()` 的输入/输出显示 `fast` 是原生 conditional edge 的分支值；视觉生成应通过 `vlm.infer + frame_sequence` 定位，不能把 route span 当成 VLM span。

- [ ] **Step 6: 运行 GREEN 并校验 authority**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window/test_window_observability.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

- [ ] **Step 7: 提交 Task 6**

```bash
git add src/assistant_agent/media/vision/observability.py \
  src/assistant_agent/media/video/realtime_video_observer.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py \
  docs/observability-harness.md \
  tests/tdd/realtime-visual-target-window/test_window_observability.py
git commit -m "feat: trace realtime visual target windows"
```

---

### Task 7: 同步当前架构 authority

**Files:**
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/runtime-event-stream-architecture.md`

- [ ] **Step 1: 更新视觉架构权威**

明确区分：

- background adaptive selection：用于持续历史，允许选择/跳帧；
- strict target window：chat 驱动，固定五帧，绕过 latest-wins pending，每帧独立 VLM；
- 前台 exact-target barrier：只等目标，ready subset 不要求连续。

- [ ] **Step 2: 更新 Media 与 runtime authority**

记录可信 content block 的 start/target 字段、4 秒 deadline、future-frame exclusion，以及 Media route 不等待 VLM/不生成回答的边界。

- [ ] **Step 3: 运行文档校验**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
git diff --check
```

- [ ] **Step 4: 提交 Task 7**

```bash
git add docs/multimodal-embedding-architecture.md \
  docs/media-agent-service-websocket.md \
  docs/runtime-event-stream-architecture.md
git commit -m "docs: define realtime visual target window semantics"
```

---

### Task 8: 最小离线验证、hot reload 与真实 Provider 验收

**Files:**
- Create: `evals/system/realtime_visual_target_window/README.md`
- Create: `evals/system/realtime_visual_target_window/runner.py`
- Create: `scripts/run_system_realtime_visual_target_window_eval.py`
- Modify: `scripts/README.md`

**Interfaces:**
- Dry-run validates configuration without Provider calls.
- Real run requires `--allow-real-provider` and five operator-supplied local frames.
- Artifact contains only sequence/status/latency/concurrency counts and trace IDs.

- [ ] **Step 1: 完成离线 TDD 验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/realtime-visual-target-window
```

Expected: PASS。不要机械运行裸 pytest；本功能不改变 core invariant。

- [ ] **Step 2: 增加显式 system eval runner**

runner 接收 `--frame-dir`，要求恰好五个按 sequence 命名的本地 JPEG；默认只 dry-run。真实运行必须同时满足：

```text
MULTIMODAL_AGENT_PROVIDER_MODE=real
--allow-real-provider
Provider config complete
operator confirms five Qwen realtime WebSocket calls
```

输出只包含：start/target、各 sequence started/finished/status、`max_concurrency`、target wait、target finish 到 Tool return 的 delta、missing sequences 和 trace IDs。不得保存图片、summary、用户问题或 raw response。

- [ ] **Step 3: 验证 runner dry-run**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_realtime_visual_target_window_eval.py --dry-run
```

Expected: 报告 `real_provider_authorized=false`，不联网。

- [ ] **Step 4: 检查并复用 8089 hot reload**

修改源码后等待 PyCharm 管理的唯一 `langgraph dev` reload；只连接 `127.0.0.1:8089`，不得另起第二个 server。检查 server log 中 reload 成功和无 import error。

- [ ] **Step 5: 在 operator 明确授权后运行真实五帧 eval**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_realtime_visual_target_window_eval.py \
  --allow-real-provider \
  --frame-dir /absolute/operator/supplied/five-frames
```

验收：五个 connection ID 不同；`max_concurrency >= 2` 且五帧均在 target 完成前已启动；若人为延迟 sequence 7，sequence 8 完成即解除 barrier；future sequence 不进入结果。若 Provider 受限无法五连接并行，应以明确 rate-limit 失败结束，不得回退共享 client。

- [ ] **Step 6: 最终检查**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/media \
  src/assistant_agent/tools/plugins/builtin/media_inspection \
  src/assistant_agent/agent_server
git diff --check
git status --short
```

- [ ] **Step 7: 提交 eval 入口**

```bash
git add evals/system/realtime_visual_target_window/README.md \
  evals/system/realtime_visual_target_window/runner.py \
  scripts/run_system_realtime_visual_target_window_eval.py \
  scripts/README.md
git commit -m "test: add realtime visual window system eval"
```

- [ ] **Step 8: 汇报**

使用权威格式：

```text
Core invariant: unchanged.
Tests: added tests/tdd/realtime-visual-target-window for temporary RED/GREEN; user may delete the directory manually.
```

列出实际执行命令。若运行了真实 Provider，单独报告授权范围、五次调用、并发/target latency 和脱敏 artifact 路径；若未获授权，明确真实验收未执行，不以 mock 结果代替。
