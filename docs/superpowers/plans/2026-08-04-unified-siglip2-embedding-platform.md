# 统一 SigLIP2 多模态表征能力实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前关键帧私有的 SigLIP2 image embedding 重构为统一 image/text 表征能力，并交付 session 短期视觉回忆、历史找物和唯一新增工具 `visual_memory_search`。

**Architecture:** 新增逻辑无状态的 `MultimodalEmbeddingProvider` 与 `EmbeddingComparator`，由 session-scoped `SessionEmbeddingCoordinator` 对同一 observation 去重并分发成功/失败事件。关键帧、时间线记忆、跨模态关联、历史找物和内部视觉关注作为独立消费者；只有历史查询通过受治理的 `visual_memory_search` 暴露给主 LLM。

**Tech Stack:** Python 3.11、Pydantic v2、ONNX Runtime GPU、NumPy、Pillow、tokenizers、pytest、现有 LangGraph runtime 与 Tool plugin 系统。

## Global Constraints

- 默认解释器固定为 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。
- pytest 必须显式设置 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得访问网络、真实 Provider 或真实 `.env`。
- 不新增依赖；Runtime 继续使用现有 `local-vision-embedding` extra，模型导出依赖仍只属于 operator 环境。
- Runtime 不下载模型或 tokenizer；真实本地 SigLIP2 只有在 `provider_mode=real` 且显式配置时启用。
- image/text tower、projection 与 tokenizer 必须来自同一不可变 revision；不同 `embedding_space_id` 禁止比较。
- 同一 observation 被多个消费者需要时只推理一次；失败不产生零向量或假 semantic score。
- 当前项目版本为 `0.1.0`；canonical embedding 配置在目标 `0.2.0` 接受旧 alias，目标 `0.3.0` 删除旧 alias，新旧值冲突始终启动失败。
- 只新增主 LLM Tool `visual_memory_search`；不新增 embedding、相似度、关键帧、入库、清理、对齐或视觉关注管理 Tool。
- `VisualAttentionConsumer` 只产出内部候选，不发送消息、不创建 durable task、不触发 proactive wake。
- 短期视觉记忆按 session + TTL 管理，媒体重连保留；session 删除、用户数据删除、TTL 或 Runtime close 必须清理。
- 查询严格遵守 request-arrival as-of 边界，不消费查询后的帧。
- 所有新 pytest 位于 `tests/tdd/unified-siglip2/`，用户可手动整目录删除；不修改 `tests/core`，除非实施证据证明 `GATE-001` 的稳定契约实际改变。
- 每个任务只提交本任务相关文件；不提交 `.local/` 模型、`.data/` 运行产物或 `.superpowers/` 视觉伴侣文件。

---

## 文件结构

新增包按职责拆分：

```text
src/assistant_agent/media/embedding/
  models.py                 # observation、success/failure、readiness 公共模型
  comparator.py             # 同空间校验与 cosine similarity
  provider.py               # Protocol、mock、factory
  local_siglip2.py          # joint manifest、image/text 预处理与 ONNX backend
  coordinator.py            # session 内去重、优先级、分发、close
  coordinator_store.py      # user/session 到 coordinator 的有界 TTL 映射
  consumers/
    keyframe.py             # image↔image 语义变化
    temporal_memory.py      # session 时间线、向量索引与视觉证据
    alignment.py            # text↔image 候选关联
    attention.py            # 内部关注候选
    object_search.py        # top-k、as-of、VLM 复核

src/assistant_agent/tools/plugins/builtin/media_inspection/
  visual_memory_tool.py     # 唯一新增 Agent Tool
```

现有 `media/video/detection/*` 在兼容期保留薄导入，不再拥有第二套模型推理事实源。

---

### Task 1: 公共 embedding 契约、Comparator 与配置迁移

**Files:**
- Create: `src/assistant_agent/media/embedding/__init__.py`
- Create: `src/assistant_agent/media/embedding/models.py`
- Create: `src/assistant_agent/media/embedding/comparator.py`
- Create: `src/assistant_agent/media/embedding/provider.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Test: `tests/tdd/unified-siglip2/test_embedding_contracts.py`
- Test: `tests/tdd/unified-siglip2/test_embedding_config.py`

**Interfaces:**
- Produces: `ImageObservation`, `TextObservation`, `EmbeddingEvent`, `EmbeddingFailureEvent`, `EmbeddingOutcome`, `EmbeddingReadiness`。
- Produces: `EmbeddingComparator.similarity(left: EmbeddingEvent, right: EmbeddingEvent) -> float`。
- Produces: `MultimodalEmbeddingProvider.embed_image/embed_text/readiness` 与 `create_multimodal_embedding_provider(config)`。
- Produces: canonical config `embedding_provider`, `siglip2_model_dir`, `embedding_cuda_device_id`；旧环境变量仅作一周期 alias。

- [ ] **Step 1: 写公共模型和 Comparator 的失败测试**

```python
def test_comparator_rejects_different_embedding_spaces() -> None:
    left = embedding_event("space-a", [1.0, 0.0], modality="image")
    right = embedding_event("space-b", [1.0, 0.0], modality="text")
    with pytest.raises(EmbeddingComparisonError, match="embedding_space_mismatch"):
        EmbeddingComparator().similarity(left, right)

def test_failure_event_has_no_vector_field() -> None:
    assert "vector" not in EmbeddingFailureEvent.model_fields
```

- [ ] **Step 2: 运行契约测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_embedding_contracts.py`

Expected: FAIL，原因是 `assistant_agent.media.embedding` 尚不存在。

- [ ] **Step 3: 实现最小公共类型和 Comparator**

```python
EmbeddingModality = Literal["image", "text"]
EmbeddingPriority = Literal["interactive", "background"]

class EmbeddingEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_id: str
    modality: EmbeddingModality
    vector: list[float] = Field(min_length=1)
    embedding_space_id: str
    model_id: str
    model_revision: str
    dimension: int = Field(gt=0)
    normalized: bool
    session_id: str
    source_observation_id: str
    video_id: str | None = None
    frame_sequence: int | None = Field(default=None, ge=0)
    captured_at_ms: int | None = None
    text_source: str | None = None
    occurred_at_ms: int | None = None
    latency_ms: int = Field(ge=0)

class EmbeddingFailureEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    modality: EmbeddingModality
    session_id: str
    source_observation_id: str
    code: str
    safe_message: str
    recoverable: bool
    latency_ms: int = Field(ge=0)

EmbeddingOutcome = EmbeddingEvent | EmbeddingFailureEvent
```

Comparator 必须拒绝空间、维度、归一化声明不兼容和非有限向量，再计算 cosine；不要在 Comparator 中加入消费者阈值。

- [ ] **Step 4: 写配置 alias 与冲突测试**

```python
def test_legacy_siglip2_env_populates_canonical_config() -> None:
    config = ProviderConfig.from_env(real_env_with(
        MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER="local_siglip2",
        SIGLIP2_VISION_MODEL_DIR="/models/legacy",
    ))
    assert config.embedding_provider == "local_siglip2"
    assert config.siglip2_model_dir == "/models/legacy"

def test_conflicting_new_and_legacy_model_dir_fails() -> None:
    with pytest.raises(ValueError, match="conflicting_siglip2_model_dir"):
        ProviderConfig.from_env(real_env_with(
            SIGLIP2_MODEL_DIR="/models/new",
            SIGLIP2_VISION_MODEL_DIR="/models/old",
        ))
```

- [ ] **Step 5: 实现 canonical 配置解析并运行 Task 1 测试**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_embedding_contracts.py tests/tdd/unified-siglip2/test_embedding_config.py`

Expected: PASS；mock mode 仍把真实 provider 选择压回 mock。

- [ ] **Step 6: 提交 Task 1**

```bash
git add src/assistant_agent/media/embedding src/assistant_agent/config/__init__.py tests/tdd/unified-siglip2
git commit -m "feat: define unified multimodal embedding contracts"
```

---

### Task 2: 联合 SigLIP2 导出资产与本地 Provider

**Files:**
- Create: `scripts/export_siglip2_embedding_onnx.py`
- Modify: `scripts/export_siglip2_vision_onnx.py`
- Create: `src/assistant_agent/media/embedding/local_siglip2.py`
- Modify: `src/assistant_agent/media/embedding/provider.py`
- Modify: `src/assistant_agent/media/video/detection/vision_embedding_provider.py`
- Modify: `src/assistant_agent/media/video/detection/local_siglip2_provider.py`
- Test: `tests/tdd/unified-siglip2/test_siglip2_joint_manifest.py`
- Test: `tests/tdd/unified-siglip2/test_siglip2_local_provider.py`
- Test: `tests/tdd/unified-siglip2/test_siglip2_export_script.py`

**Interfaces:**
- Consumes: Task 1 的 observation、outcome、readiness 和 canonical config。
- Produces: `Siglip2EmbeddingManifest`、`load_siglip2_embedding_manifest(path)`、`LocalSiglip2EmbeddingProvider`。
- Produces joint ONNX 文件 `vision_model.onnx`、`text_model.onnx`、tokenizer 资产和 schema v2 `manifest.json`。

- [ ] **Step 1: 写 joint/image-only manifest RED 测试**

```python
def test_joint_manifest_requires_one_revision_and_space(tmp_path: Path) -> None:
    manifest = write_joint_manifest(tmp_path, image_revision="a" * 40, text_revision="b" * 40)
    with pytest.raises(LocalSiglip2Error, match="manifest_model_revision_mismatch"):
        load_siglip2_embedding_manifest(manifest.parent)

def test_image_only_manifest_reports_text_unavailable(tmp_path: Path) -> None:
    provider = LocalSiglip2EmbeddingProvider(config_for(write_image_manifest(tmp_path)))
    assert provider.readiness().image_ready is True
    assert provider.readiness().text_ready is False
```

- [ ] **Step 2: 运行 manifest/provider 测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_siglip2_joint_manifest.py tests/tdd/unified-siglip2/test_siglip2_local_provider.py`

Expected: FAIL，缺少 joint loader/provider。

- [ ] **Step 3: 实现 schema v2 manifest 与 image/text backend**

`LocalSiglip2EmbeddingProvider` 必须实现：

```python
def embed_image(self, observation: ImageObservation) -> EmbeddingOutcome:
    return self._embed_image_with_validated_manifest(observation)
def embed_text(self, observation: TextObservation) -> EmbeddingOutcome:
    return self._embed_text_with_validated_manifest(observation)
def readiness(self) -> EmbeddingReadiness:
    return self._validated_readiness()
```

上述三个方法在 `LocalSiglip2EmbeddingProvider` 中分别完成真实 image 推理、真实 text 推理和无推理
readiness 检查；禁止保留 `pass` 或 `NotImplementedError`。

image preprocessing 固定 manifest 的 resize/mean/std；text preprocessing 固定已校验 tokenizer、padding、truncation 和 `max_length`。两个 backend 输出都执行有限值校验、维度校验和 L2 normalization。ONNX session 必须把 CPU fallback 关闭。

现有 DashScope image embedding adapter 通过新 Provider Protocol 保留 image readiness；在没有同空间 text
契约证据时明确报告 `text_ready=False`，不得与本地 SigLIP2 text embedding 混用。旧
`vision_embedding_provider.py` 只保留兼容导入和旧调用形状到新 image API 的适配。

- [ ] **Step 4: 写导出脚本 RED 测试**

```python
def test_joint_export_manifest_names_both_projections() -> None:
    manifest = module.build_joint_manifest(
        model_id="google/siglip2-base-patch16-224",
        model_revision="a" * 40,
        dimension=768,
        image_model_file="vision_model.onnx",
        image_sha256="b" * 64,
        image_external_data={"vision_model.onnx.data": "c" * 64},
        text_model_file="text_model.onnx",
        text_sha256="d" * 64,
        text_external_data={"text_model.onnx.data": "e" * 64},
        tokenizer_file="tokenizer.json",
        tokenizer_sha256="f" * 64,
        max_length=64,
    )
    assert manifest["supported_modalities"] == ["image", "text"]
    assert manifest["image"]["projection"] == "visual_projection"
    assert manifest["text"]["projection"] == "text_projection"
    assert manifest["embedding_space_id"].endswith(":joint-projection-v1")
```

- [ ] **Step 5: 实现 joint exporter 与旧脚本兼容入口**

新脚本从同一 `AutoModel` 导出 `get_image_features` 与 `get_text_features`。旧 `export_siglip2_vision_onnx.py` 只转发到新脚本的 `main()` 并在 stderr 输出 deprecated 信息；不复制导出逻辑。

- [ ] **Step 6: 运行 Task 2 测试与现有 SigLIP2 测试**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_siglip2_joint_manifest.py tests/tdd/unified-siglip2/test_siglip2_local_provider.py tests/tdd/unified-siglip2/test_siglip2_export_script.py tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`

Expected: PASS；测试只使用合成 ONNX/假 backend，不加载真实 GPU 模型。

- [ ] **Step 7: 提交 Task 2**

```bash
git add scripts/export_siglip2_embedding_onnx.py scripts/export_siglip2_vision_onnx.py src/assistant_agent/media/embedding src/assistant_agent/media/video/detection/vision_embedding_provider.py src/assistant_agent/media/video/detection/local_siglip2_provider.py tests/tdd/unified-siglip2 tests/tdd/siglip2-keyframe
git commit -m "feat: add joint siglip2 image text provider"
```

---

### Task 3: Session coordinator、去重、优先级与消费者隔离

**Files:**
- Create: `src/assistant_agent/media/embedding/coordinator.py`
- Create: `src/assistant_agent/media/embedding/coordinator_store.py`
- Test: `tests/tdd/unified-siglip2/test_embedding_coordinator.py`
- Test: `tests/tdd/unified-siglip2/test_embedding_coordinator_store.py`

**Interfaces:**
- Consumes: `MultimodalEmbeddingProvider` 与 Task 1 outcome。
- Produces: `EmbeddingConsumer` Protocol、`SessionEmbeddingCoordinator.embed_image/embed_text/register_consumer/close`。
- Produces: `SessionEmbeddingCoordinatorStore.resolve/clear_session/clear_user/close`。

- [ ] **Step 1: 写并发去重和失败不缓存 RED 测试**

```python
def test_same_observation_concurrent_calls_share_one_provider_result() -> None:
    provider = BlockingProvider()
    coordinator = SessionEmbeddingCoordinator("session-1", provider)
    outcomes = run_two_threads(lambda: coordinator.embed_image(image_observation("frame-1")))
    assert provider.image_calls == 1
    assert outcomes[0] is outcomes[1]

def test_failure_is_dispatched_but_not_added_to_success_cache() -> None:
    provider = FailingThenSuccessfulProvider()
    coordinator = SessionEmbeddingCoordinator("session-1", provider)
    assert isinstance(coordinator.embed_text(text_observation("q1")), EmbeddingFailureEvent)
    assert isinstance(coordinator.embed_text(text_observation("q1")), EmbeddingEvent)
```

- [ ] **Step 2: 运行 coordinator 测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_embedding_coordinator.py`

Expected: FAIL，缺少 coordinator。

- [ ] **Step 3: 实现 coordinator**

```python
class EmbeddingConsumer(Protocol):
    consumer_id: str
    def accept(
        self,
        outcome: EmbeddingEvent | EmbeddingFailureEvent,
        observation: ImageObservation | TextObservation,
    ) -> None:
        raise NotImplementedError
    def close(self) -> None:
        raise NotImplementedError

class SessionEmbeddingCoordinator:
    def embed_image(self, observation: ImageObservation, *, priority: EmbeddingPriority = "background") -> EmbeddingOutcome:
        return self._compute_and_dispatch(observation, priority=priority)
    def embed_text(self, observation: TextObservation, *, priority: EmbeddingPriority = "interactive") -> EmbeddingOutcome:
        return self._compute_and_dispatch(observation, priority=priority)
    def register_consumer(self, consumer: EmbeddingConsumer) -> None:
        self._consumer_workers.add(consumer)
    def close(self) -> None:
        self._close_workers_and_clear_cache()
```

用 `Future` + lock 合并相同 computation key。成功结果使用有界 LRU 短缓存；失败只分发不缓存。协调器
把 outcome 与其原始 observation 成对交给 consumer，使视觉证据引用无需进入公共 embedding 结果。
每个 consumer 使用独立有界 worker queue，`accept` 只入队；队列策略为构造参数中的
`latest_wins|drop_oldest|reject_new`。

- [ ] **Step 4: 写 store TTL/clear RED 测试并实现 store**

```python
def test_store_reuses_session_and_clears_owned_coordinator() -> None:
    store = SessionEmbeddingCoordinatorStore(factory=coordinator_factory)
    first = store.resolve("user-1", "session-1")
    assert store.resolve("user-1", "session-1") is first
    assert store.clear_session("user-1", "session-1") is True
    assert first.closed is True
```

- [ ] **Step 5: 运行 Task 3 测试**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_embedding_coordinator.py tests/tdd/unified-siglip2/test_embedding_coordinator_store.py`

Expected: PASS，并证明慢/异常 consumer 不阻塞其他 consumer。

- [ ] **Step 6: 提交 Task 3**

```bash
git add src/assistant_agent/media/embedding/coordinator.py src/assistant_agent/media/embedding/coordinator_store.py tests/tdd/unified-siglip2
git commit -m "feat: coordinate session embedding inference"
```

---

### Task 4: 迁移语义关键帧为共享结果消费者

**Files:**
- Create: `src/assistant_agent/media/embedding/consumers/__init__.py`
- Create: `src/assistant_agent/media/embedding/consumers/keyframe.py`
- Modify: `src/assistant_agent/media/video/detection/semantic_detector.py`
- Modify: `src/assistant_agent/media/video/keyframe/collector.py`
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Test: `tests/tdd/unified-siglip2/test_keyframe_consumer.py`
- Test: `tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`

**Interfaces:**
- Consumes: session coordinator 和 Comparator。
- Produces: `KeyframeChangeConsumer.compare(current, reference) -> SemanticChangeResult`。
- Preserves: 现有首帧、SSIM 触发、2 FPS 保底 probe、最大间隔和 0.4/0.6 组合策略。

- [ ] **Step 1: 写“一次推理，多消费者可见”RED 测试**

```python
def test_keyframe_probe_uses_coordinator_once_and_dispatches_same_event() -> None:
    coordinator, provider, recorder = coordinated_fixture()
    detector = SemanticChangeDetector(coordinator=coordinator)
    detector.compare(frame("f1", 0.0), None, semantic_candidate=True)
    assert provider.image_calls == 1
    assert recorder.events[0].source_observation_id == "f1"
```

- [ ] **Step 2: 运行关键帧新旧测试并确认新测试 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_keyframe_consumer.py tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`

Expected: 新测试 FAIL；现有测试仍是迁移基线。

- [ ] **Step 3: 实现 keyframe consumer 并迁移 observer 注入**

`RealtimeVideoObserver` 新增必需的 `embedding_coordinator` 参数；`_create_realtime_video_observer()` 从 Runtime 的 coordinator store 以 user/session resolve。`SemanticChangeDetector` 不再自行 factory-load Provider，只把 `VideoFrame` 归一化成 `ImageObservation` 后调用 coordinator。

- [ ] **Step 4: 运行 Task 4 测试**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_keyframe_consumer.py tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py tests/tdd/realtime-video-as-of/test_realtime_video_as_of.py`

Expected: PASS；semantic failure 仍 fail closed 为 0，SSIM 路径继续工作。

- [ ] **Step 5: 提交 Task 4**

```bash
git add src/assistant_agent/media/embedding/consumers src/assistant_agent/media/video/detection/semantic_detector.py src/assistant_agent/media/video/keyframe/collector.py src/assistant_agent/media/video/realtime_video_observer.py src/assistant_agent/api/agent_service_websocket.py tests/tdd/unified-siglip2 tests/tdd/siglip2-keyframe tests/tdd/realtime-video-as-of
git commit -m "refactor: share siglip2 keyframe embeddings"
```

---

### Task 5: Session temporal memory、视觉证据 retention 与清理

**Files:**
- Create: `src/assistant_agent/media/embedding/consumers/temporal_memory.py`
- Modify: `src/assistant_agent/media/embedding/coordinator_store.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/assistant_runtime_app.py`
- Modify: `src/assistant_agent/gateway/runtime_pool.py`
- Test: `tests/tdd/unified-siglip2/test_temporal_visual_memory.py`
- Test: `tests/tdd/unified-siglip2/test_embedding_runtime_lifecycle.py`

**Interfaces:**
- Produces: `TemporalVisualRecord`、`TemporalVisualMemory.search_candidates()`、`has_history()`、`clear()`。
- Produces: session store factory 自动注册一个 `TemporalMemoryConsumer`。
- Preserves: 当前 `RealtimeVideoMemoryStore` 继续保存 VLM rolling snapshot；新时间线只保存 embedding 检索事实和 owned evidence。

- [ ] **Step 1: 写 retention、重连和清理 RED 测试**

```python
def test_temporal_memory_keeps_probed_frame_not_selected_as_keyframe(tmp_path: Path) -> None:
    memory = TemporalVisualMemory(root=tmp_path, max_records=4, max_bytes=4096)
    memory.accept(
        image_event("probe-1", sequence=1),
        image_observation("probe-1", sequence=1, image_ref=jpeg(tmp_path)),
    )
    assert [item.frame_sequence for item in memory.records()] == [1]

def test_clear_removes_index_and_owned_jpeg(tmp_path: Path) -> None:
    memory = populated_memory(tmp_path)
    owned = memory.records()[0].evidence_ref
    memory.clear()
    assert memory.records() == []
    assert not Path(owned).exists()
```

- [ ] **Step 2: 运行 temporal memory 测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_temporal_visual_memory.py`

Expected: FAIL，缺少 temporal memory。

- [ ] **Step 3: 实现有界时间线和视觉证据 ownership**

`TemporalMemoryConsumer.accept()` 在返回前用同文件系统原子 hard-link 把 live source JPEG 变成
session-owned evidence，再把轻量记录放入内部队列；原始窗口随后 unlink 不影响 owned inode。live
frame root 与 temporal evidence root 都位于仓库 `.data/`，不做跨文件系统字节复制。hard-link 失败时
记录 retention failure、不加入索引，从而避免慢 I/O 阻塞 Provider 或媒体 ACK。淘汰按记录数和总字节
双上限原子删除记录与 owned link。

- [ ] **Step 4: 接入 Runtime/store 共享与 delete cleanup**

`AgentGraphRuntime` 接受并暴露共享 `embedding_coordinator_store`；`shared_gateway_runtime_factory()` 传递同一 store。`AssistantRuntimeApp.delete_session()`、`delete_user_runtime_data()` 和 runtime pool close 分别调用 `clear_session`、`clear_user`、`close`。WebSocket transport close 不调用 temporal clear，从而支持同 session 重连。

- [ ] **Step 5: 运行 Task 5 测试**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_temporal_visual_memory.py tests/tdd/unified-siglip2/test_embedding_runtime_lifecycle.py`

Expected: PASS；session 删除与 TTL 后向量/JPEG 均为 0。

- [ ] **Step 6: 提交 Task 5**

```bash
git add src/assistant_agent/media/embedding/consumers/temporal_memory.py src/assistant_agent/media/embedding/coordinator_store.py src/assistant_agent/runtime/runtime.py src/assistant_agent/runtime/assistant_runtime_app.py src/assistant_agent/gateway/runtime_pool.py tests/tdd/unified-siglip2
git commit -m "feat: retain bounded session visual memory"
```

---

### Task 6: 跨模态关联与内部视觉关注消费者

**Files:**
- Create: `src/assistant_agent/media/embedding/consumers/alignment.py`
- Create: `src/assistant_agent/media/embedding/consumers/attention.py`
- Modify: `src/assistant_agent/media/embedding/coordinator_store.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Test: `tests/tdd/unified-siglip2/test_cross_modal_alignment.py`
- Test: `tests/tdd/unified-siglip2/test_visual_attention_consumer.py`
- Test: `tests/tdd/unified-siglip2/test_runtime_text_observation.py`

**Interfaces:**
- Produces: `CrossModalAlignmentConsumer.align(text_event, image_events) -> list[CrossModalAlignment]`。
- Produces: `VisualAttentionConsumer.set_internal_target()`、`observe()` 与 `candidate_events()`；不产生 Tool 或外部副作用。

- [ ] **Step 1: 写时间邻近与空间拒绝 RED 测试**

```python
def test_alignment_orders_by_similarity_then_temporal_distance() -> None:
    result = consumer.align(text_event(at_ms=1000), [image_event(at_ms=900), image_event(at_ms=9000)])
    assert result[0].image_observation_id == "near-frame"

def test_alignment_rejects_mismatched_spaces() -> None:
    assert consumer.align(text_event(space="a"), [image_event(space="b")]) == []
```

- [ ] **Step 2: 写关注消费者无副作用 RED 测试**

```python
def test_attention_only_emits_internal_candidate() -> None:
    candidate = consumer.observe(relevant_image_event())
    assert candidate.kind == "visual_attention_candidate"
    assert not hasattr(consumer, "send_message")
    assert not hasattr(consumer, "create_task")
```

- [ ] **Step 3: 实现两个消费者并运行测试**

协调器 store 的 session factory 注册 alignment consumer 和无关注目标的 attention consumer。Runtime 在
建立 `AgentState` 后，把非空稳定 `request.text` 规范化为 `TextObservation`；只有该 session 协调器
报告至少一个 text consumer 时才调用 `embed_text`。source observation id 使用本轮 `run_id`，时间使用
Runtime 接收本轮的 wall clock；文本不自动写入 `TemporalVisualMemory` 或 Mem0。

新增运行时测试：

```python
def test_runtime_embeds_stable_text_only_when_session_has_text_consumer() -> None:
    runtime = runtime_with_recording_coordinator(text_consumers=1)
    runtime.run_state(user_request(text="刚才的钥匙在哪里"))
    assert runtime.recording_coordinator.text_observations[0].source == "user_request"

def test_runtime_does_not_embed_empty_text_or_write_it_to_temporal_memory() -> None:
    runtime = runtime_with_recording_coordinator(text_consumers=1)
    runtime.run_state(user_request(text=""))
    assert runtime.recording_coordinator.text_observations == []
    assert runtime.temporal_memory.text_records() == []
```

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_cross_modal_alignment.py tests/tdd/unified-siglip2/test_visual_attention_consumer.py tests/tdd/unified-siglip2/test_runtime_text_observation.py`

Expected: PASS；所有权重/阈值通过构造配置注入，不写入 Provider。

- [ ] **Step 4: 提交 Task 6**

```bash
git add src/assistant_agent/media/embedding/consumers/alignment.py src/assistant_agent/media/embedding/consumers/attention.py src/assistant_agent/media/embedding/coordinator_store.py src/assistant_agent/runtime/runtime.py tests/tdd/unified-siglip2
git commit -m "feat: add internal cross modal consumers"
```

---

### Task 7: 历史找物服务与 VLM 复核

**Files:**
- Create: `src/assistant_agent/media/embedding/consumers/object_search.py`
- Test: `tests/tdd/unified-siglip2/test_visual_memory_search_service.py`

**Interfaces:**
- Consumes: coordinator 的 text embedding、TemporalVisualMemory、Comparator、`VisionUnderstandingClient`。
- Produces: `VisualMemorySearchRequest`、`VisualMemorySearchResult`、`VisualMemorySearchService.search()`。

- [ ] **Step 1: 写 top-k、as-of 与复核失败 RED 测试**

```python
def test_search_never_returns_frame_after_as_of_boundary() -> None:
    result = service.search(request(query="钥匙", as_of_sequence=10))
    assert all(match.frame_sequence <= 10 for match in result.matches)

def test_vlm_failure_keeps_embedding_hit_as_candidate() -> None:
    result = service_with_failing_vlm().search(request(query="钥匙"))
    assert result.status == "candidate"
    assert result.verification_status == "failed"
```

- [ ] **Step 2: 运行 search service 测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_visual_memory_search_service.py`

Expected: FAIL，缺少 search service。

- [ ] **Step 3: 实现检索与 VLM 复核**

```python
class VisualMemorySearchService:
    def search(self, request: VisualMemorySearchRequest) -> VisualMemorySearchResult:
        query_outcome = self.coordinator.embed_text(request.text_observation, priority="interactive")
        # failure -> unavailable；success -> temporal top-k -> as-of filter -> VLM verify
```

结果状态固定为 `confirmed|candidate|uncertain|not_found|unavailable`。VLM 请求只携带 top-k owned frame refs 和当前 query；VLM raw response 不进入结果。全局 embedding 只召回，不输出坐标。

- [ ] **Step 4: 运行 Task 7 测试**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_visual_memory_search_service.py`

Expected: PASS，并覆盖 confirmed、candidate、not_found、unavailable。

- [ ] **Step 5: 提交 Task 7**

```bash
git add src/assistant_agent/media/embedding/consumers/object_search.py tests/tdd/unified-siglip2
git commit -m "feat: search session visual history"
```

---

### Task 8: `visual_memory_search` Tool、Plugin 装配与结构化暴露

**Files:**
- Create: `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/contracts.py`
- Modify: `src/assistant_agent/tools/plugins/registry_factory.py`
- Modify: `src/assistant_agent/tools/ids.py`
- Modify: `src/assistant_agent/context/tool_exposure.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/system_prompt_policy.py`
- Test: `tests/tdd/unified-siglip2/test_visual_memory_tool.py`
- Test: `tests/tdd/unified-siglip2/test_visual_memory_tool_gating.py`

**Interfaces:**
- Consumes: Task 7 search service 与 session temporal memory。
- Produces: Tool name `visual_memory_search`，模型只拥有 `query/time_window/search_mode`。
- Runtime owns: `session_id`；ToolContext owns trusted `as_of_sequence/video_generation`。

`VisualMemorySearchInput` 的模型字段固定为：

```python
class VisualMemoryTimeWindow(BaseModel):
    lookback_seconds: int | None = Field(default=None, ge=1, le=3600)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)

class VisualMemorySearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    time_window: VisualMemoryTimeWindow | None = None
    search_mode: Literal["auto", "object", "scene", "event"] = "auto"
    session_id: str = ""  # runtime binding，模型 schema 中移除
```

- [ ] **Step 1: 写 Tool schema/治理 RED 测试**

```python
def test_visual_memory_tool_exposes_only_model_owned_fields() -> None:
    schema = registry.get_spec("visual_memory_search").input_schema
    assert set(schema["properties"]) == {"query", "time_window", "search_mode"}

def test_visual_memory_tool_runs_through_validator_executor_registry() -> None:
    result = execute_governed_tool("visual_memory_search", {"query": "钥匙"})
    assert result.success is True
    assert result.data["status"] == "confirmed"
```

- [ ] **Step 2: 写结构化暴露 RED 测试**

```python
def test_history_tool_exposed_when_runtime_marks_session_history_available() -> None:
    request = trusted_request_without_active_video()
    runtime.refresh_visual_memory_capability(request)
    assert "visual_memory_search" in selected_tool_names(request)

def test_user_metadata_cannot_forge_visual_memory_availability() -> None:
    request = untrusted_request(metadata={"_trusted_visual_memory_available": True})
    runtime.refresh_visual_memory_capability(request)
    assert "visual_memory_search" not in selected_tool_names(request)
```

- [ ] **Step 3: 实现 Tool 与 Plugin 依赖注入**

`VisualMemorySearchInput` 中 `session_id` 使用 `RuntimeInputBinding(source="runtime_identity", key="session_id")`。
Tool 声明 `category="read"`、`requires_media=[]`，因为同 session 媒体断线后仍允许查询已有历史；是否
暴露由可信 history capability 决定。Tool 从 `ToolContext.metadata["request_metadata"]` 读取 Runtime
覆盖后的 as-of/generation，不接受模型字段。`ToolPluginContext` 新增 search service/store 依赖，
MediaInspectionPlugin 在 dependency ready 时注册该 Tool。

- [ ] **Step 4: 实现 Runtime-owned capability 刷新**

在 tool catalog 构建前，Runtime 必须先删除调用方传入的 `_trusted_visual_memory_available`，再根据 `(user_id, session_id)` temporal store 的 `has_history()` 写入布尔值。`tool_exposure_facts()` 只信这个 Runtime 覆盖后的字段；不能检查 `request.text`。

- [ ] **Step 5: 运行 Tool/gating 测试及现有 live-view gating**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_visual_memory_tool.py tests/tdd/unified-siglip2/test_visual_memory_tool_gating.py tests/tdd/live-view-tool-gating/test_live_view_tool_gating.py`

Expected: PASS；现有 `live_view_inspect` 与内部 `realtime_video_observe` 行为不变。

- [ ] **Step 6: 提交 Task 8**

```bash
git add src/assistant_agent/tools/plugins/builtin/media_inspection src/assistant_agent/tools/plugins/contracts.py src/assistant_agent/tools/plugins/registry_factory.py src/assistant_agent/tools/ids.py src/assistant_agent/context/tool_exposure.py src/assistant_agent/runtime/runtime.py src/assistant_agent/runtime/system_prompt_policy.py tests/tdd/unified-siglip2 tests/tdd/live-view-tool-gating
git commit -m "feat: expose governed visual memory search"
```

---

### Task 9: 观测契约与本地 system eval

**Files:**
- Create: `src/assistant_agent/media/embedding/observability.py`
- Create: `evals/system/multimodal_embedding/__init__.py`
- Create: `evals/system/multimodal_embedding/runner.py`
- Create: `evals/system/multimodal_embedding/README.md`
- Create: `scripts/run_system_multimodal_embedding_eval.py`
- Modify: `scripts/README.md`
- Test: `tests/tdd/unified-siglip2/test_embedding_observability.py`

**Interfaces:**
- Produces safe events `embedding.requested/deduplicated/started/finished/failed/dispatched/consumer_dropped/session_cleanup`。
- Produces explicit local runner with `--dry-run` and `--allow-local-model` gates。

- [ ] **Step 1: 写 redaction RED 测试**

```python
def test_embedding_trace_excludes_vector_text_and_paths() -> None:
    payload = embedding_trace_payload(event_with_secret_fields())
    assert "vector" not in payload
    assert "text" not in payload
    assert "image_ref" not in payload
    assert payload["embedding_space_id_digest"]
```

- [ ] **Step 2: 实现安全观测并运行测试**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2/test_embedding_observability.py`

Expected: PASS。

- [ ] **Step 3: 实现 system eval runner**

Runner 必须在 dry-run 中只报告资产路径是否配置和预期检查；只有 `--allow-local-model` 才创建 CUDA session。正式运行验证 image/text readiness、共同 space、固定输入可重复、正样本排序高于负样本、CUDA provider 位于首位且 CPU fallback 关闭。结果写入 `.data/evals/system/multimodal_embedding/<run>/`，不写 vector、文本或图片内容。

- [ ] **Step 4: 验证 runner dry-run**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_system_multimodal_embedding_eval.py --dry-run`

Expected: exit 0，输出 `status=dry_run`，不加载真实模型。

- [ ] **Step 5: 提交 Task 9**

```bash
git add src/assistant_agent/media/embedding/observability.py evals/system/multimodal_embedding scripts/run_system_multimodal_embedding_eval.py scripts/README.md tests/tdd/unified-siglip2
git commit -m "feat: observe and evaluate multimodal embeddings"
```

---

### Task 10: Agent eval、权威文档与全量验收

**Files:**
- Create: `docs/multimodal-embedding-architecture.md`
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/context_engineering_status.md`
- Create: `evals/agent/tasks/visual_memory_last_seen_object/__init__.py`
- Create: `evals/agent/tasks/visual_memory_last_seen_object/task.json`
- Create: `evals/agent/tasks/visual_memory_last_seen_object/environment.py`
- Create: `evals/agent/tasks/visual_memory_last_seen_object/grader.py`
- Create: `evals/agent/tasks/visual_memory_last_seen_object/calibration.json`
- Create: `evals/agent/tasks/visual_memory_not_found_honesty/__init__.py`
- Create: `evals/agent/tasks/visual_memory_not_found_honesty/task.json`
- Create: `evals/agent/tasks/visual_memory_not_found_honesty/environment.py`
- Create: `evals/agent/tasks/visual_memory_not_found_honesty/grader.py`
- Create: `evals/agent/tasks/visual_memory_not_found_honesty/calibration.json`
- Modify: `evals/agent/suites.json`

**Interfaces:**
- Produces: 当前事实权威 `docs/multimodal-embedding-architecture.md`。
- Produces: 两个首批 Task，分别验证历史找物成功与未找到时诚实回答；其他质量 case 在后续按同一 Task 模板增加，不扩大本期 Tool 数量。

- [ ] **Step 1: 添加两个受控 Agent Task**

成功 Task 的 Environment 提供 session temporal index、确定性 `visual_memory_search` outcome 和完整默认工具目录；grader 要求工具成功、回答引用正确时间/场景且不声称精确坐标。未找到 Task 固定返回 `status=not_found`，grader 要求不编造目标存在。

- [ ] **Step 2: inspect Task 定义**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_agent_evals.py --inspect --task visual_memory_last_seen_object`

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_agent_evals.py --inspect --task visual_memory_not_found_honesty`

Expected: 两个命令 exit 0；Environment outcome、工具 visibility 和四分 grader 契约完整。不要在本任务自动运行真实 Judge。

- [ ] **Step 3: 写权威文档并同步路由**

文档必须明确：Provider/Comparator/coordinator/五消费者、只有一个新增 Tool、ASR 只是 text 来源、semantic probe 不是 2 FPS 上限、session retention/as-of、image-only readiness、失败语义、配置迁移、system/Agent eval 命令和非目标。

- [ ] **Step 4: 运行 feature 最小验证集**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/unified-siglip2 tests/tdd/siglip2-keyframe tests/tdd/realtime-video-as-of tests/tdd/live-view-tool-gating`

Expected: 全部 PASS。

- [ ] **Step 5: 因共享 runtime/config/tool 基础设施发生变化，运行默认 core 安全网**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q`

Expected: 全部 PASS；若失败，只修复本次引入的回归，不扩大 core invariant。

- [ ] **Step 6: 做静态交付审计**

Run: `rg -n "siglip2_vision_model_dir|vision_embedding_provider|create_vision_embedding_provider" src scripts docs tests/tdd/unified-siglip2 tests/tdd/siglip2-keyframe`

Expected: 旧名称只存在于明确的兼容解析、deprecated wrapper、迁移测试或兼容说明中；生产消费者不再调用旧 image-only factory。

Run: `rg -n "visual_memory_search|visual_attention_manage|siglip2_embed|find_object" src/assistant_agent/tools docs/multimodal-embedding-architecture.md`

Expected: 注册的新主 LLM Tool 只有 `visual_memory_search`；`visual_attention_manage`、`siglip2_embed*`、`find_object` 没有被注册。

- [ ] **Step 7: 提交 Task 10**

```bash
git add AGENTS.md README.md docs/multimodal-embedding-architecture.md docs/media-agent-service-websocket.md docs/runtime-event-stream-architecture.md docs/tool-calling-architecture.md docs/context_engineering_status.md evals/agent/tasks/visual_memory_last_seen_object evals/agent/tasks/visual_memory_not_found_honesty evals/agent/suites.json
git commit -m "docs: publish multimodal embedding architecture"
```

---

## 完成汇报格式

最终汇报必须包含：

```text
Core invariant: unchanged.
Tests: added/updated tests/tdd/unified-siglip2 for temporary RED/GREEN; user may delete the directory manually.
```

并列出：

- 实际执行的 pytest、system eval dry-run 和 Agent Task inspect 命令；
- 是否调用真实本地 SigLIP2/CUDA；若调用，说明 operator gate、模型 revision 和验证结果；
- 未运行的真实 Judge/Provider eval 及原因；
- 新增产品功能仅为短期视觉回忆与历史找物；新增 Agent Tool 仅为 `visual_memory_search`；
- 配置 alias 的删除版本；
- session cleanup、as-of 与重复推理率的验证证据。
