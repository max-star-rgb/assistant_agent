# 全语义实时视觉与短期语义记忆实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Pixel/SSIM 混合选帧与全采样帧 image-vector 时间线迁移为固定 5 FPS、无积压的纯语义关键帧流水线，并让实时画面与历史查询统一读取成功 VLM 记录。

**Architecture:** 原始帧先经纯时间 admission，再进入一个 in-flight、一个 pending 的 latest-wins semantic pipeline；每个实际执行帧共享一次 SigLIP2 image embedding，只有 initial/semantic/max-interval/interactive 帧进入受治理 VLM。成功 VLM 结果被规范化、text-embedded 并写入 session-scoped `SessionVisualSemanticStore`，`live_view_inspect` 与 `visual_memory_search` 均读取该 store，查询不再二次调用 VLM。

**Tech Stack:** Python 3.11、asyncio、Pydantic v2、ONNX Runtime GPU、现有 SigLIP2 provider、VisionUnderstandingClient、Tool plugin/runtime/Gateway、pytest。

## Global Constraints

- 默认解释器固定为 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。
- pytest 显式设置 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得访问网络、真实 Provider、Mem0 或真实 `.env`。
- 不新增依赖，不下载模型，不自动调用真实 CUDA/VLM/Judge。
- 语义入口默认 5 FPS；semantic pipeline 始终最多一个 in-flight 和一个 pending slot。
- pending 只能是 latest-wins background 或 pinned interactive；不得形成第三层积压。
- 删除 Pixel Diff、SSIM、AdaptiveSampler、结构/组合分数与全采样帧 `TemporalVisualMemory`。
- 关键帧 reason 只允许 `initial|semantic|max_interval|interactive`。
- 只有 schema 有效、来源可信的成功 VLM 结果可以创建 `VisualSemanticRecord`。
- live/history 必须读取同一个 store；`visual_memory_search` 查询时不得调用 VLM。
- Mem0 不接收视觉记录；正常 user/assistant turn capture 的长期记忆链路保持不变。
- query、文本、向量、JPEG/base64、绝对路径和 Provider raw response不进入日志、trace 或 Tool schema。
- `REALTIME_KEYFRAME_SEMANTIC_PROBE_FPS` 作为新 FPS 配置 alias 保留至不早于 `0.3.0`，冲突值启动失败。
- TDD 只更新 `tests/tdd/unified-siglip2/`、`tests/tdd/siglip2-keyframe/`、`tests/tdd/realtime-video-as-of/`。
- 工作区存在用户并行 3D rendering/API/docs 改动；每次只暂存精确文件并检查 staged diff。

---

## 文件结构

```text
src/assistant_agent/media/video/
  semantic_pipeline.py       # 时间 admission、单槽 pending、interactive pin、embedding worker
  semantic_store.py          # VisualSemanticRecord、snapshot、evidence、历史查询
  semantic_store_pool.py     # (user_id, session_id) store ownership、TTL 与清理
  realtime_video_observer.py # 接入 pipeline、执行 VLM、发布统一记录
  keyframe/selector.py       # 纯 semantic initial/min/max/interactive 决策

src/assistant_agent/media/embedding/consumers/
  object_search.py           # text-to-text 查询，不依赖 VLM
```

实施后删除生产路径中的 `frame_difference.py`、`ssim_detector.py`、`adaptive_sampler.py`、旧 collector 与 `temporal_memory.py`。

---

### Task 1: 配置迁移与纯语义关键帧策略

**Files:**
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/media/video/keyframe/selector.py`
- Test: `tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`
- Test: `tests/tdd/unified-siglip2/test_embedding_config.py`

**Interfaces:**
- Produces `semantic_input_fps=5.0`、`keyframe_min_interval_seconds=0.5`。
- Retains `keyframe_semantic_threshold=0.18`、`keyframe_max_interval_seconds=10.0`。
- Produces `SemanticKeyframeSelector.select(event, frame_timestamp_seconds, force_interactive)` 与 `force_due(frame_timestamp_seconds)`。

- [ ] **Step 1: 写 RED 测试**

```python
def test_semantic_input_defaults_to_five_fps() -> None:
    config = ProviderConfig.from_env({"MULTIMODAL_AGENT_PROVIDER_MODE": "mock"})
    assert config.semantic_input_fps == 5.0
    assert config.keyframe_min_interval_seconds == 0.5

def test_conflicting_semantic_input_alias_fails() -> None:
    with pytest.raises(ValueError, match="conflicting_semantic_input_fps"):
        ProviderConfig.from_env({
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "REALTIME_SEMANTIC_INPUT_FPS": "5",
            "REALTIME_KEYFRAME_SEMANTIC_PROBE_FPS": "2",
        })

def test_selector_uses_only_embedding_change() -> None:
    selector = SemanticKeyframeSelector(SemanticKeyframeConfig())
    assert selector.select(event("a", [1.0, 0.0]), frame_timestamp_seconds=0.0).reason == "initial"
    assert selector.select(event("b", [0.0, 1.0]), frame_timestamp_seconds=1.0).reason == "semantic"
```

- [ ] **Step 2: 运行并确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2/test_embedding_config.py tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py
```

Expected: FAIL，缺少新配置和 event-based selector。

- [ ] **Step 3: 实现最小策略**

```python
@dataclass(frozen=True)
class SemanticKeyframeConfig:
    min_interval_seconds: float = 0.5
    max_interval_seconds: float = 10.0
    semantic_threshold: float = 0.18

class SemanticKeyframeSelector:
    def force_due(self, frame_timestamp_seconds: float) -> bool:
        return self._last_selected_at is None or (
            frame_timestamp_seconds - self._last_selected_at >= self.config.max_interval_seconds
        )

    def select(self, event, *, frame_timestamp_seconds, force_interactive=False):
        if force_interactive:
            return self._commit(event, frame_timestamp_seconds, "interactive")
        if self._last_event is None:
            return self._commit(event, frame_timestamp_seconds, "initial")
        elapsed = frame_timestamp_seconds - self._last_selected_at
        if elapsed >= self.config.max_interval_seconds:
            return self._commit(event, frame_timestamp_seconds, "max_interval")
        change = 1.0 - self.comparator.similarity(event, self._last_event)
        if elapsed >= self.config.min_interval_seconds and change >= self.config.semantic_threshold:
            return self._commit(event, frame_timestamp_seconds, "semantic", semantic_change=change)
        return SemanticKeyframeDecision(selected=False, reason="below_threshold", semantic_change=change)
```

解析必须拒绝非正 FPS、负 interval、`min > max` 和范围外阈值。显式设置结构/组合阈值返回 `removed_realtime_keyframe_config`。

- [ ] **Step 4: 重跑 Step 2，确认 PASS**

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/config/__init__.py src/assistant_agent/media/video/keyframe/selector.py \
  tests/tdd/unified-siglip2/test_embedding_config.py tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py
git diff --cached --check
git commit -m "refactor: define semantic-only keyframe policy"
```

---

### Task 2: 固定时间 admission 与有界 semantic pipeline

**Files:**
- Create: `src/assistant_agent/media/video/semantic_pipeline.py`
- Modify: `src/assistant_agent/media/video/__init__.py`
- Test: `tests/tdd/siglip2-keyframe/test_semantic_frame_pipeline.py`

**Interfaces:**
- Produces `FixedIntervalSemanticSampler.admit(sequence, now) -> bool`。
- Produces async `SemanticFramePipeline.submit/promote/wait_idle/close`。
- Selected callback: `on_selected(frame, event: EmbeddingEvent | None, reason) -> Awaitable[None]`；event 只在 embedding 失败的 interactive/max-interval fallback 为空。

- [ ] **Step 1: 写 RED 测试**

```python
def test_sampler_admits_at_five_fps_without_pixels() -> None:
    sampler = FixedIntervalSemanticSampler(fps=5.0)
    assert sampler.admit(sequence=1, now=0.0)
    assert not sampler.admit(sequence=2, now=0.1)
    assert sampler.admit(sequence=3, now=0.2)

@pytest.mark.asyncio
async def test_pending_is_latest_wins() -> None:
    pipeline, provider = blocked_pipeline()
    await pipeline.submit(frame(1))
    await provider.started.wait()
    await pipeline.submit(frame(2))
    result = await pipeline.submit(frame(3))
    assert result.replaced_sequence == 2
    provider.release.set()
    await pipeline.wait_idle()
    assert provider.sequences == [1, 3]

@pytest.mark.asyncio
async def test_interactive_pending_is_not_replaced() -> None:
    pipeline, provider = blocked_pipeline()
    await pipeline.submit(frame(1))
    await provider.started.wait()
    await pipeline.promote(frame(7))
    assert (await pipeline.submit(frame(8))).reason == "interactive_pending"

@pytest.mark.asyncio
async def test_embedding_failure_still_allows_due_vlm_refresh() -> None:
    pipeline, selected = pipeline_with_failing_embedding(last_selected_at=0.0)
    await pipeline.submit(frame(10, timestamp_seconds=10.0))
    await pipeline.wait_idle()
    assert selected == [(10, None, "max_interval")]
```

- [ ] **Step 2: 运行并确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/siglip2-keyframe/test_semantic_frame_pipeline.py
```

Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现 pipeline**

```python
async def submit(self, frame: VideoFrame) -> SemanticAdmission:
    if not self.sampler.admit(sequence=frame.sequence, now=self.clock()):
        return SemanticAdmission(admitted=False, reason="fixed_interval")
    retained = await asyncio.to_thread(self._retain_frame, frame)
    return await self._put(SemanticFrameJob(frame=retained, priority="background"))

async def promote(self, frame: VideoFrame) -> SemanticAdmission:
    retained = await asyncio.to_thread(self._retain_frame, frame)
    return await self._put(SemanticFrameJob(frame=retained, priority="interactive", pinned=True))
```

admission 在 ACK 前 hard-link JPEG，避免原始三帧窗口先淘汰文件。替换、失败且未触发 fallback、未选中时删除 owned link；选中时转移给 VLM stage。worker 用 `asyncio.to_thread(coordinator.embed_image, observation)`。embedding failure 仅在 interactive 或 `selector.force_due()` 时以 `event=None` 调用 selected callback，不生成假向量。

- [ ] **Step 4: 重跑 Step 2，确认 PASS 且无临时文件残留**

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/media/video/semantic_pipeline.py src/assistant_agent/media/video/__init__.py \
  tests/tdd/siglip2-keyframe/test_semantic_frame_pipeline.py
git diff --cached --check
git commit -m "feat: add bounded semantic frame pipeline"
```

---

### Task 3: 统一视觉语义记录与 session store

**Files:**
- Create: `src/assistant_agent/media/video/semantic_store.py`
- Create: `src/assistant_agent/media/video/semantic_store_pool.py`
- Modify: `src/assistant_agent/media/video/__init__.py`
- Test: `tests/tdd/unified-siglip2/test_visual_semantic_store.py`

**Interfaces:**
- Produces `VisualSemanticRecord`、`VisualSemanticSnapshot`、`VisualSemanticCandidate`。
- Produces `SessionVisualSemanticStore.record_success/record_failure/mark_pending/latest/at_or_before/wait_for_sequence/search/clear/close`。
- Produces `SessionVisualSemanticStorePool.resolve/peek/clear_session/clear_user/close`，key 固定为 `(user_id, session_id)`。
- Defaults: 256 records、256 MiB evidence、与 coordinator store 相同的 1800 秒 idle TTL。

- [ ] **Step 1: 写 RED 测试**

```python
def test_one_record_serves_latest_and_history(tmp_path: Path) -> None:
    store = SessionVisualSemanticStore(root=tmp_path / "visual")
    store.record_success(record(sequence=7, scene="厨房", objects=["钥匙"]))
    assert store.latest("video-1").frame_sequence == 7
    assert store.at_or_before("video-1", sequence=7).objects == ["钥匙"]
    assert store.search(query_event(), video_id="video-1", as_of_sequence=7)[0].record.frame_sequence == 7

def test_as_of_excludes_future_record(tmp_path: Path) -> None:
    store = SessionVisualSemanticStore(root=tmp_path / "visual")
    store.record_success(record(sequence=7))
    store.record_success(record(sequence=9))
    assert [x.record.frame_sequence for x in store.search(query_event(), as_of_sequence=7)] == [7]

def test_eviction_deletes_owned_evidence(tmp_path: Path) -> None:
    store = SessionVisualSemanticStore(root=tmp_path / "visual", max_records=1)
    first = store.record_success(record(sequence=1))
    store.record_success(record(sequence=2))
    assert Path(first.evidence_ref).exists() is False

def test_pool_isolates_same_session_id_between_users(tmp_path: Path) -> None:
    pool = semantic_store_pool(tmp_path)
    assert pool.resolve("user-a", "session-1") is not pool.resolve("user-b", "session-1")

def test_pool_ttl_eviction_closes_store_and_deletes_evidence(tmp_path: Path) -> None:
    clock = MutableClock(0.0)
    pool = semantic_store_pool(tmp_path, ttl_seconds=30.0, clock=clock)
    evidence = add_record(pool.resolve("user-1", "session-1"))
    clock.value = 31.0
    assert pool.peek("user-1", "session-1") is None
    assert evidence.exists() is False
```

- [ ] **Step 2: 运行并确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2/test_visual_semantic_store.py
```

Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现不可变 record 与线程安全 store**

```python
class VisualSemanticRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    record_id: str
    session_id: str
    video_id: str
    frame_sequence: int = Field(ge=0)
    captured_at_ms: int | None = Field(default=None, ge=0)
    summary: str = ""
    scene: str | None = None
    objects: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    text_in_video: list[str] = Field(default_factory=list)
    search_embedding: list[float] | None = Field(default=None, exclude=True)
    embedding_space_id: str | None = None
    index_status: Literal["ready", "unavailable"]
    evidence_ref: str = Field(exclude=True)
    evidence_bytes: int = Field(ge=0)
    created_at_ms: int = Field(ge=0)
```

同时保留现有 live projection 所需的 products、brands、colors、materials、timestamps、style_tags、confidence、provider、model。Store 在 condition/lock 内维护 sequence history、pending/failure；成功发布前创建 session-owned hard-link，retention 失败不得改变 latest/history。`search` 只比较 `index_status=ready` 且 embedding space 兼容的记录。

- [ ] **Step 4: 重跑 Step 2，确认 PASS**

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/media/video/semantic_store.py \
  src/assistant_agent/media/video/semantic_store_pool.py src/assistant_agent/media/video/__init__.py \
  tests/tdd/unified-siglip2/test_visual_semantic_store.py
git diff --cached --check
git commit -m "feat: add session visual semantic store"
```

---

### Task 4: Observer 接入 semantic pipeline 并发布 VLM 记录

**Files:**
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`
- Modify: `tests/tdd/realtime-video-as-of/test_realtime_video_as_of.py`
- Test: `tests/tdd/unified-siglip2/test_realtime_visual_semantic_publication.py`

**Interfaces:**
- Consumes Task 2 pipeline、Task 3 store。
- `submit()` 快速返回 admission；`promote()` 产生 pinned interactive job。
- VLM success 规范化、text embed 后发布 `VisualSemanticRecord`。

- [ ] **Step 1: 写 RED 测试**

```python
@pytest.mark.asyncio
async def test_submit_returns_before_embedding_finishes(tmp_path: Path) -> None:
    observer, provider = observer_with_blocked_embedding(tmp_path)
    result = await observer.submit(frame(1))
    assert result.semantic_admission == "admitted"
    assert provider.finished.is_set() is False

@pytest.mark.asyncio
async def test_successful_vlm_publishes_indexed_record(tmp_path: Path) -> None:
    observer, store = observer_with_successful_vlm(tmp_path)
    await observer.submit(frame(1))
    await observer.wait_idle()
    record = store.latest("video-1")
    assert record.objects == ["钥匙"]
    assert record.index_status == "ready"

@pytest.mark.asyncio
async def test_invalid_vlm_result_is_not_published(tmp_path: Path) -> None:
    observer, store = observer_with_invalid_vlm(tmp_path)
    await observer.submit(frame(1))
    await observer.wait_idle()
    assert store.latest("video-1") is None
```

- [ ] **Step 2: 运行并确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2/test_realtime_visual_semantic_publication.py \
  tests/tdd/realtime-video-as-of/test_realtime_video_as_of.py
```

Expected: FAIL，observer 仍同步调用旧 collector，并写旧 rolling snapshot。

- [ ] **Step 3: 重构 observer 两阶段 worker**

selected callback 把 retained frame 放入现有单 in-flight、单 latest-pending VLM 队列。VLM success 后执行：

```python
text_outcome = self.embedding_coordinator.embed_text(
    TextObservation(
        session_id=self.session_id,
        observation_id=f"visual-record:{item.record.sequence}",
        text=build_visual_search_text(result),
        source="visual_semantic_record",
        occurred_at_ms=item.record.timestamp_ms,
    ),
    priority="background",
)
record = VisualSemanticRecord.from_vlm_result(
    session_id=self.session_id,
    video_id=video_id,
    frame=item.record,
    result=result,
    text_embedding=text_outcome if isinstance(text_outcome, EmbeddingEvent) else None,
)
self.semantic_store.record_success(record)
```

text embedding 失败时记录仍以 `index_status=unavailable` 发布给 live view，但不进入 history searchable subset。`wait_idle()` 等待 semantic/VLM 两个 stage；`close()` 清空 pending 并删除尚未转移的 owned 文件。

- [ ] **Step 4: 更新 chat target 与 ACK 语义**

chat 冻结最新原始 sequence，调用 `pin_sequence()` 和 `promote()`；sequence 7 的 interactive target 不得被后到 sequence 8 替换。视频 ACK 改为“解码并完成 semantic admission”，不再承诺本地选帧已经完成。

- [ ] **Step 5: 运行并确认 GREEN**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2/test_realtime_visual_semantic_publication.py \
  tests/tdd/siglip2-keyframe tests/tdd/realtime-video-as-of
```

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/media/video/realtime_video_observer.py \
  src/assistant_agent/api/agent_service_websocket.py tests/tdd/siglip2-keyframe \
  tests/tdd/realtime-video-as-of tests/tdd/unified-siglip2/test_realtime_visual_semantic_publication.py
git diff --cached --check
git commit -m "refactor: process realtime video through semantic pipeline"
```

---

### Task 5: Runtime、Plugin 与 live view 统一 store

**Files:**
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/gateway/runtime_pool.py`
- Modify: `src/assistant_agent/tools/plugins/contracts.py`
- Modify: `src/assistant_agent/tools/plugins/registry_factory.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Test: `tests/tdd/unified-siglip2/test_live_view_semantic_store.py`
- Test: `tests/tdd/realtime-video-as-of/test_realtime_video_as_of.py`

**Interfaces:**
- Runtime owns `visual_semantic_store_pool: SessionVisualSemanticStorePool`。
- LiveViewInspectTool consumes `semantic_store_pool` and resolves trusted user/session，不再依赖全局 `RealtimeVideoMemoryStore`。

- [ ] **Step 1: 写 RED 测试**

```python
def test_live_view_reads_semantic_store_without_provider(tmp_path: Path) -> None:
    store = populated_store(tmp_path, sequence=12, scene="厨房", objects=["钥匙"])
    tool = LiveViewInspectTool(client=FailIfCalledVisionClient(), semantic_store_pool=pool_with(store))
    result = tool.run(live_context(target_sequence=12))
    assert result.model_observation["scene"] == "厨房"

def test_runtime_and_observer_share_store() -> None:
    runtime = AgentGraphRuntime(config=mock_config())
    observer = create_observer_from_runtime(runtime)
    assert observer.semantic_store is runtime.visual_semantic_store_pool.peek("user-1", "session-1")
```

- [ ] **Step 2: 运行并确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2/test_live_view_semantic_store.py
```

Expected: FAIL，runtime/plugin 仍依赖旧 store。

- [ ] **Step 3: 迁移依赖注入和 projection**

把 runtime、runtime pool、`ToolPluginContext`、registry factory、MediaInspectionPlugin、Agent-Service observer factory 的依赖统一命名为 `visual_semantic_store_pool`。observer 创建时 resolve 当前 user/session store；Tool 执行时从可信 ToolContext peek 同一 entry。`VideoUnderstandingBranch` 从 `VisualSemanticSnapshot` 投影既有 prompt-safe 字段，保留 target sequence、gap、pending、failure 和 freshness。非 Agent-Service 显式视频仍走 VisionUnderstandingClient。

- [ ] **Step 4: 运行并确认 GREEN**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2/test_live_view_semantic_store.py tests/tdd/realtime-video-as-of
```

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/runtime/runtime.py src/assistant_agent/gateway/runtime_pool.py \
  src/assistant_agent/tools/plugins/contracts.py src/assistant_agent/tools/plugins/registry_factory.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py \
  src/assistant_agent/api/agent_service_websocket.py tests/tdd/realtime-video-as-of \
  tests/tdd/unified-siglip2/test_live_view_semantic_store.py
git diff --cached --check
git commit -m "refactor: unify live view on visual semantic store"
```

---

### Task 6: 历史查询迁移为 VLM 文本记录检索

**Files:**
- Modify: `src/assistant_agent/media/embedding/consumers/object_search.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `tests/tdd/unified-siglip2/test_visual_memory_search_service.py`
- Modify: `tests/tdd/unified-siglip2/test_visual_memory_tool.py`
- Modify: `tests/tdd/unified-siglip2/test_visual_memory_tool_gating.py`

**Interfaces:**
- Service consumes coordinator、session semantic store、`visual_memory_candidate_similarity=0.20`、`visual_memory_confirmed_similarity=0.30`；Tool 通过 pool 按可信 user/session 取 store。
- Removes `VisionUnderstandingClient`、`verification_top_k` 和 query-time `understand()`。
- Tool schema 保持 `query/time_window/search_mode`，session/as-of 仍由 Runtime 绑定。

- [ ] **Step 1: 写 RED 测试**

```python
def test_search_reads_indexed_vlm_record_without_vision_client(tmp_path: Path) -> None:
    result = service_with_record(tmp_path, similarity=0.35).search(request(query="钥匙"))
    assert result.status == "confirmed"
    assert result.matches[0].verified_scene == "厨房台面"

def test_fixed_similarity_statuses(tmp_path: Path) -> None:
    assert service_with_record(tmp_path, similarity=0.25).search(request()).status == "candidate"
    assert service_with_record(tmp_path, similarity=0.19).search(request()).status == "not_found"

def test_tool_constructor_has_no_vision_client() -> None:
    assert "vision_client" not in inspect.signature(VisualMemorySearchTool).parameters

def test_visual_memory_similarity_thresholds_are_configurable() -> None:
    config = ProviderConfig.from_env({
        "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
        "REALTIME_VISUAL_MEMORY_CANDIDATE_SIMILARITY": "0.22",
        "REALTIME_VISUAL_MEMORY_CONFIRMED_SIMILARITY": "0.34",
    })
    assert config.visual_memory_candidate_similarity == 0.22
    assert config.visual_memory_confirmed_similarity == 0.34
```

- [ ] **Step 2: 运行并确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2/test_visual_memory_search_service.py \
  tests/tdd/unified-siglip2/test_visual_memory_tool.py \
  tests/tdd/unified-siglip2/test_visual_memory_tool_gating.py
```

Expected: FAIL，旧 service/tool 仍依赖 image timeline 和 VLM。

- [ ] **Step 3: 实现 text-to-text 查询**

```python
query = coordinator.embed_text(
    TextObservation(
        session_id=request.session_id,
        observation_id=request.request_id,
        text=request.query.strip(),
        source="visual_memory_search",
    ),
    priority="interactive",
)
candidates = semantic_store.search(
    query,
    as_of_sequence=request.as_of_sequence,
    since_ms=request.since_ms,
    until_ms=request.until_ms,
    top_k=request.top_k,
)
```

最高 similarity `>= confirmed threshold` 为 confirmed，`>= candidate threshold` 为 candidate，否则 not_found；配置必须满足 `-1 <= candidate < confirmed <= 1`。query embedding 失败为 unavailable。Tool 只返回 sequence、captured time、similarity、scene、objects、actions、events，不返回 evidence path、search text 或 vector。

- [ ] **Step 4: 更新 exposure 和 plugin**

Runtime 仅在 `visual_semantic_store_pool.peek(user_id, session_id)` 返回的 store 具有 searchable history 时写 trusted availability。Plugin 直接注入 coordinator store 与 semantic store pool，删除历史查询的 vision client。

- [ ] **Step 5: 重跑 Step 2，确认 PASS**

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/config/__init__.py \
  src/assistant_agent/media/embedding/consumers/object_search.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py src/assistant_agent/runtime/runtime.py \
  tests/tdd/unified-siglip2/test_visual_memory_search_service.py \
  tests/tdd/unified-siglip2/test_visual_memory_tool.py \
  tests/tdd/unified-siglip2/test_visual_memory_tool_gating.py
git diff --cached --check
git commit -m "refactor: search indexed visual semantic records"
```

---

### Task 7: 删除旧选帧与 image-vector 时间线

**Files:**
- Delete: `src/assistant_agent/media/video/detection/frame_difference.py`
- Delete: `src/assistant_agent/media/video/detection/ssim_detector.py`
- Delete: `src/assistant_agent/media/video/sampling/adaptive_sampler.py`
- Delete: `src/assistant_agent/media/video/keyframe/collector.py`
- Delete: `src/assistant_agent/media/embedding/consumers/temporal_memory.py`
- Modify: package `__init__.py` files under `media/video` and `media/embedding/consumers`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`
- Replace: `tests/tdd/unified-siglip2/test_temporal_visual_memory.py` with `test_visual_semantic_store_lifecycle.py`
- Modify: `tests/tdd/unified-siglip2/test_embedding_runtime_lifecycle.py`

**Interfaces:**
- Removes旧 detector/sampler/collector/temporal memory 类型。
- Coordinator 只保留 alignment/attention consumers 与共享 inference/cache。

- [ ] **Step 1: 写 lifecycle RED 测试**

```python
def test_runtime_coordinator_has_no_image_timeline() -> None:
    runtime = AgentGraphRuntime(config=mock_config())
    coordinator = runtime.embedding_coordinator_store.resolve("user-1", "session-1")
    assert hasattr(coordinator, "temporal_visual_memory") is False

def test_session_clear_removes_records_and_evidence(tmp_path: Path) -> None:
    runtime, evidence = runtime_with_semantic_record(tmp_path)
    assert runtime.clear_session_visual_state("user-1", "session-1") is True
    assert evidence.exists() is False
```

- [ ] **Step 2: 运行并确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2/test_embedding_runtime_lifecycle.py \
  tests/tdd/unified-siglip2/test_visual_semantic_store_lifecycle.py
```

- [ ] **Step 3: 删除模块并清理引用**

```bash
rg -n "FrameDifferenceDetector|SSIMChangeDetector|AdaptiveFrameSampler|AdaptiveKeyframeCollector|TemporalVisualMemory|TemporalMemoryConsumer|keyframe_structural_threshold|keyframe_combined_threshold" \
  src/assistant_agent scripts docs/*.md tests/tdd
```

生产源码匹配必须为零。Runtime coordinator factory 只注册 alignment/attention；session/user/TTL/runtime close 清理由 semantic store 执行。旧 TDD 文件改写为新事实，不删除整个 feature 目录。

- [ ] **Step 4: 运行迁移后的 feature tests**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2 tests/tdd/siglip2-keyframe tests/tdd/realtime-video-as-of
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/media src/assistant_agent/runtime/runtime.py \
  tests/tdd/unified-siglip2 tests/tdd/siglip2-keyframe tests/tdd/realtime-video-as-of
git diff --cached --check
git commit -m "refactor: remove structural visual memory paths"
```

---

### Task 8: 可观测性、评测、权威文档与最终验证

**Files:**
- Modify: `src/assistant_agent/media/embedding/observability.py`
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `scripts/run_system_multimodal_embedding_eval.py`
- Modify: `evals/agent/task_support.py`
- Modify: both visual-memory Agent eval environments
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/context_engineering_status.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/observability-harness.md`
- Modify: `README.md`、`scripts/README.md`、`evals/README.md`
- Test: `tests/tdd/unified-siglip2/test_embedding_observability.py`

**Interfaces:**
- Safe events: admitted/skipped/replaced、queue latency、keyframe reason、record retained/evicted/index_failed、query status/count/latency。
- Eval dry-run checks fixed admission、bounded queue、VLM text index、no query-time VLM。

- [ ] **Step 1: 写观测 RED 测试**

```python
def test_pipeline_event_excludes_sensitive_payloads() -> None:
    observer = RecordingEmbeddingObserver()
    emit_semantic_pipeline_observation(
        observer,
        "semantic_frame.replaced",
        session_id="session-secret",
        sequence=7,
        image_ref="/secret/frame.jpg",
    )
    payload = observer.events[0]
    assert payload["sequence"] == 7
    assert "image_ref" not in payload
    assert "session-secret" not in json.dumps(payload)
```

- [ ] **Step 2: 运行并确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2/test_embedding_observability.py
```

- [ ] **Step 3: 实现安全事件并更新 eval**

system eval dry-run 的 `would_check` 固定为 image/text readiness、shared space、fixed 5 FPS admission、bounded latest-wins、semantic ranking、VLM text index、visual-memory text ranking、query no-VLM、CUDA-first、CPU-fallback-disabled。两个 Agent eval environment 预装成功/空 `VisualSemanticRecord`，不再伪造查询时 VLM verification。

- [ ] **Step 4: 更新当前权威文档**

文档明确：5 FPS 是 input cap；Pixel/SSIM 已删除；每个实际执行帧都 embedding；只有成功 VLM record 进入短期记忆；live/history 共用 store；查询 text-to-text 且无二次 VLM；Mem0 仅长期事实；旧 probe FPS 只是 alias。

- [ ] **Step 5: 运行完整离线验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-siglip2 tests/tdd/siglip2-keyframe tests/tdd/realtime-video-as-of \
  tests/tdd/live-view-tool-gating

MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q

/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_system_multimodal_embedding_eval.py --dry-run
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_agent_evals.py --inspect --task visual_memory_last_seen_object
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_agent_evals.py --inspect --task visual_memory_not_found_honesty
```

Expected: pytest 全部 PASS；dry-run 返回 `local_model_loaded=false`；两个 environment validation passed。不运行真实 system eval、Chat Provider、Judge 或 Langfuse publish。

- [ ] **Step 6: 静态审计与提交**

```bash
rg -n "FrameDifferenceDetector|SSIMChangeDetector|AdaptiveFrameSampler|AdaptiveKeyframeCollector|TemporalVisualMemory|TemporalMemoryConsumer" \
  src/assistant_agent scripts docs/*.md tests/tdd
rg -n "vision_client|understand\(" src/assistant_agent/media/embedding/consumers/object_search.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py
git diff --check
```

第一条只允许 removal migration tests；生产代码无匹配。第二条无匹配。暂存时列出精确文件，确认不含用户 3D rendering 改动后提交 `docs: publish semantic visual memory architecture`。

---

## 完成判定

- 产品能力仍只有 session 短期视觉回忆和历史找物；Agent Tool 仍只有 `visual_memory_search`。
- 原始媒体输入不形成 embedding backlog；默认每 200ms 最多接纳一帧。
- 每个实际执行帧只调用一次共享 image embedding。
- Pixel/SSIM/AdaptiveSampler/image-vector timeline 不再存在于生产路径。
- live view 和 historical search 读取同一个成功 VLM record store。
- 历史查询不调用 VLM、不使用 Mem0、不跨 session、不越过 as-of。
- session/user/TTL/runtime close 清理 record、text vector 与 evidence。
- 所有离线验证通过；真实 Provider 仍由 operator gate 控制。
