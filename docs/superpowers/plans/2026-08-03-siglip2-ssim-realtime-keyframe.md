# SigLIP2 + SSIM 实时关键帧 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让实时视频 observer 使用本地 SigLIP2 image tower 与 SSIM 选择关键帧，最大静态间隔为 10 秒，并保留未来同向量空间 text tower 的扩展契约。

**Architecture:** `RealtimeVideoObserver` 从 Runtime 的可信 `ProviderConfig` 构造 `AdaptiveKeyframeCollector`。collector 对输入帧计算 SSIM，并按 2 FPS 固定语义探测节奏或结构变化候选调用本地 SigLIP2 vision ONNX；selector 允许 SSIM、semantic、组合分数和 10 秒静态间隔独立触发。SigLIP2 provider 只暴露 `embed_image()`，结构化返回 `embedding_space_id` 等元数据，后续可在同 provider family 增加 `embed_text()`，本轮不加载文本塔。

**Tech Stack:** Python 3.12、Pydantic/dataclass、NumPy、Pillow、ONNX Runtime GPU、pytest（mock/offline）、Hugging Face SigLIP2 模型资产。

## Global Constraints

- 模型固定为 `google/siglip2-base-patch16-224`，Runtime 当前只加载 vision ONNX，不加载 tokenizer 或 text tower。
- 最大静态关键帧间隔固定为默认值 10 秒。
- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock` 不加载模型、不访问网络；本地 SigLIP2 必须显式配置并提供完整本地资产。
- Runtime 请求路径禁止自动下载模型；模型权重、ONNX 文件、缓存和 embedding 数值不写入 Git、prompt 或 trace。
- SSIM、semantic 和组合分数均可触发；初始阈值分别为 `0.35`、`0.18`、`0.25`，组合权重为 `0.4/0.6`。
- 固定语义探测初始值为 2 FPS；缺少依赖、资产或 CUDA 时结构化降级为 SSIM-only，禁止以直方图冒充 semantic。
- `live_view_inspect` 的可信工具暴露、A 时刻目标冻结、最多等待 10 秒及不得消费 A 之后结果的语义不变。
- 不修改 `tests/core`；临时 RED/GREEN 测试放在 `tests/tdd/siglip2-keyframe/`，真实 GPU 时延不由 pytest 证明。

---

### Task 1: 配置与 image-only embedding 契约

**Files:**
- Create: `tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/media/video/detection/vision_embedding_provider.py`

**Interfaces:**
- Produces: `VisionEmbeddingProvider.embed_image(frame: VideoFrame) -> VisionEmbeddingResult`
- Produces: `VisionEmbeddingResult.embedding_space_id/model_family/model_revision/dimension/normalized`
- Produces: `ProviderConfig.vision_embedding_provider == "local_siglip2"` 及本地模型路径/device/阈值/探测 FPS 配置。

- [ ] **Step 1: 写配置和结果元数据的失败测试**

```python
def test_local_siglip2_config_is_explicit_and_preserves_embedding_space(monkeypatch):
    config = ProviderConfig.from_env({
        "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
        "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER": "local_siglip2",
        "SIGLIP2_VISION_MODEL_DIR": "/models/siglip2",
    })
    assert config.vision_embedding_provider == "local_siglip2"
    assert config.siglip2_vision_model_dir == "/models/siglip2"
    assert config.keyframe_max_interval_seconds == 10.0
    assert config.keyframe_semantic_probe_fps == 2.0

    result = VisionEmbeddingResult(
        embedding=[1.0, 0.0], provider="local_siglip2",
        model="google/siglip2-base-patch16-224",
        model_family="siglip2", model_revision="revision-sentinel",
        embedding_space_id="siglip2-base-p16-224@revision-sentinel:vision-pool-v1",
        dimension=2, normalized=True,
    )
    assert result.embedding_space_id.startswith("siglip2-base")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`

Expected: FAIL，`ProviderConfig` 尚无本地 SigLIP2 字段且 `VisionEmbeddingResult` 尚无 embedding-space 元数据。

- [ ] **Step 3: 实现配置和契约**

```python
VisionEmbeddingProviderName = Literal["mock", "dashscope", "local_siglip2"]

@dataclass(frozen=True)
class ProviderConfig:
    siglip2_vision_model_dir: str | None = None
    siglip2_cuda_device_id: int = 0
    keyframe_max_interval_seconds: float = 10.0
    keyframe_semantic_probe_fps: float = 2.0
    keyframe_structural_threshold: float = 0.35
    keyframe_semantic_threshold: float = 0.18
    keyframe_combined_threshold: float = 0.25

class VisionEmbeddingProvider(Protocol):
    def embed_image(self, frame: VideoFrame) -> VisionEmbeddingResult:
        """Return one structured image embedding result."""
```

让 `MockVisionEmbeddingProvider`、`DashScopeVisionEmbeddingProvider` 和测试 metadata model 统一实现 `embed_image`。`from_env()` 只在 real mode 且显式值为 `local_siglip2` 时选择真实本地 provider；mock mode 继续归一化为 `mock`。所有数值配置使用既有 `_float_env/_int_env` 并保持正值校验。

- [ ] **Step 4: 运行目标测试并确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`

Expected: PASS，且没有模型加载或网络访问。

- [ ] **Step 5: 提交本任务文件**

```bash
git add src/assistant_agent/config/__init__.py \
  src/assistant_agent/media/video/detection/vision_embedding_provider.py \
  tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py
git commit -m "feat: define local siglip2 embedding contract"
```

### Task 2: 本地 SigLIP2 vision ONNX provider

**Files:**
- Create: `src/assistant_agent/media/video/detection/local_siglip2_provider.py`
- Modify: `src/assistant_agent/media/video/detection/vision_embedding_provider.py`
- Modify: `pyproject.toml`
- Modify: `tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`

**Interfaces:**
- Consumes: Task 1 的 `VisionEmbeddingResult` 和 `embed_image()` 协议。
- Produces: `LocalSiglip2VisionConfig`、`LocalSiglip2VisionProvider.embed_image()`、`create_vision_embedding_provider()` 的 `local_siglip2` 分支。

- [ ] **Step 1: 写 manifest、归一化和 fail-closed 的失败测试**

```python
def test_local_siglip2_provider_returns_normalized_image_embedding(tmp_path):
    _write_manifest(tmp_path, dimension=3, checksum="checksum-sentinel")
    backend = FakeSiglipBackend(output=[[3.0, 4.0, 0.0]])
    provider = LocalSiglip2VisionProvider(
        LocalSiglip2VisionConfig(model_dir=tmp_path), backend=backend,
        checksum_validator=lambda *_: True,
    )
    result = provider.embed_image(_frame_with_rgb_fixture(tmp_path))
    assert result.embedding == pytest.approx([0.6, 0.8, 0.0])
    assert result.normalized is True
    assert result.embedding_space_id.endswith(":vision-pool-v1")

def test_local_siglip2_provider_fails_closed_when_assets_are_missing(tmp_path):
    provider = LocalSiglip2VisionProvider(LocalSiglip2VisionConfig(model_dir=tmp_path))
    result = provider.embed_image(_frame_with_rgb_fixture(tmp_path))
    assert result.embedding == []
    assert result.errors[0]["code"] == "local_model_unavailable"
```

- [ ] **Step 2: 运行新增测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`

Expected: FAIL，local provider 尚不存在。

- [ ] **Step 3: 实现惰性共享 CUDA provider**

```python
@dataclass(frozen=True)
class LocalSiglip2VisionConfig:
    model_dir: Path
    cuda_device_id: int = 0

class LocalSiglip2VisionProvider:
    provider = "local_siglip2"
    model = "google/siglip2-base-patch16-224"

    def embed_image(self, frame: VideoFrame) -> VisionEmbeddingResult:
        try:
            manifest = self._assets.load_and_validate()
            pixels = self._preprocessor.to_pixel_values(frame, manifest)
            pooled = self._backend.run_image(pixels)
            embedding = l2_normalize(pooled)
            return VisionEmbeddingResult(
                embedding=embedding,
                provider=self.provider,
                model=self.model,
                model_family="siglip2",
                model_revision=manifest.model_revision,
                embedding_space_id=manifest.embedding_space_id,
                dimension=len(embedding),
                normalized=True,
            )
        except LocalSiglip2Error as exc:
            return failed_local_embedding(exc.code, exc.safe_message)
```

同一文件定义 `Siglip2VisionManifest`、`Siglip2AssetLoader`、`Siglip2ImagePreprocessor`、
`OnnxSiglip2ImageBackend.run_image(pixel_values) -> list[float]`、`l2_normalize()`、
`LocalSiglip2Error` 和 `failed_local_embedding()`。ONNX Runtime、NumPy 和 Pillow 必须在 local
provider 初始化时惰性 import；模块 import 本身不能要求这些可选依赖。以规范化绝对 `model_dir`
和 device id 为 key 加锁缓存 session。错误统一转换为 `provider_unconfigured`、
`local_model_unavailable`、`local_model_integrity_failed` 或 `local_model_inference_failed`，消息不包含
完整本地路径。

在 `pyproject.toml` 增加：

```toml
local-vision-embedding = [
  "numpy>=2.0,<3",
  "Pillow>=10,<12",
  "onnxruntime-gpu>=1.20,<2",
]
```

- [ ] **Step 4: 运行测试并确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`

Expected: PASS；测试通过注入 fake backend，不加载 CUDA、不访问网络。

- [ ] **Step 5: 提交本任务文件**

```bash
git add pyproject.toml \
  src/assistant_agent/media/video/detection/local_siglip2_provider.py \
  src/assistant_agent/media/video/detection/vision_embedding_provider.py \
  tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py
git commit -m "feat: add local siglip2 vision embeddings"
```

### Task 3: SSIM + SigLIP2 独立触发与 10 秒静态间隔

**Files:**
- Modify: `src/assistant_agent/media/video/detection/semantic_detector.py`
- Modify: `src/assistant_agent/media/video/keyframe/selector.py`
- Modify: `src/assistant_agent/media/video/keyframe/collector.py`
- Modify: `tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`

**Interfaces:**
- Consumes: `embed_image(frame)` 和 Task 1 的阈值配置。
- Produces: `KeyframeSelectorConfig(structural_threshold, semantic_threshold, combined_threshold, structural_weight, semantic_weight, max_interval_seconds)`。
- Produces: collector 的 2 FPS semantic probe 调度与选中 keyframe embedding 缓存。

- [ ] **Step 1: 写四种触发路径的失败测试**

```python
@pytest.mark.parametrize(("metrics", "reason"), [
    (KeyframeChangeMetrics(structural_change_score=.36), "structural_change"),
    (KeyframeChangeMetrics(semantic_change_score=.19), "semantic_change"),
    (KeyframeChangeMetrics(structural_change_score=.20, semantic_change_score=.30), "combined_change"),
])
def test_selector_allows_independent_and_combined_triggers(metrics, reason):
    selector = SemanticKeyframeSelector(KeyframeSelectorConfig(min_interval_seconds=0.0))
    decision = selector.select(_frame_at(1.0), selector.with_score(metrics), last_keyframe_at=0.0)
    assert decision.selected is True
    assert decision.reason == reason

def test_selector_forces_static_keyframe_at_ten_seconds():
    selector = SemanticKeyframeSelector(KeyframeSelectorConfig(min_interval_seconds=0.0))
    before = selector.select(_frame_at(9.999), KeyframeChangeMetrics(), last_keyframe_at=0.0)
    due = selector.select(_frame_at(10.0), KeyframeChangeMetrics(), last_keyframe_at=0.0)
    assert before.selected is False
    assert due.reason == "max_interval"
```

另加 collector 测试：SSIM 未过阈值但固定 2 FPS probe 到期时调用注入的 image embedder并允许 semantic 触发；probe 未到期时不调用；选择关键帧后 reference embedding 只计算一次。

- [ ] **Step 2: 运行新增测试并确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`

Expected: FAIL，当前 selector 只看统一加权 threshold、默认最大间隔 8 秒且 realtime override 为 2 秒。

- [ ] **Step 3: 实现独立阈值和语义探测节奏**

```python
@dataclass(frozen=True)
class KeyframeSelectorConfig:
    min_interval_seconds: float = 0.5
    max_interval_seconds: float = 10.0
    structural_threshold: float = 0.35
    semantic_threshold: float = 0.18
    combined_threshold: float = 0.25
    structural_weight: float = 0.4
    semantic_weight: float = 0.6

def score(self, metrics):
    return (metrics.structural_change_score * self.config.structural_weight
            + metrics.semantic_change_score * self.config.semantic_weight)
```

`select()` 在 min interval 之后依次处理 max interval、combined、structural、semantic。若 combined
成立且 structural 和 semantic 都独立越阈值，使用 `structural_and_semantic_change`；若 combined
成立但仅一个单项越阈值，使用 `combined_change`。这样保留已批准阈值，同时让原因确定且纯 SSIM、
纯 semantic 仍可独立触发。collector 对每个 ingress 先算 SSIM；首帧、结构候选、距上次 semantic
probe 至少 `1 / probe_fps` 或 max interval 到期时才调用 image embedding。pixel difference 只保留
在采样诊断，不进入最终 score。

- [ ] **Step 4: 运行测试并确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`

Expected: PASS，所有触发原因和边界时间精确匹配。

- [ ] **Step 5: 提交本任务文件**

```bash
git add src/assistant_agent/media/video/detection/semantic_detector.py \
  src/assistant_agent/media/video/keyframe/selector.py \
  src/assistant_agent/media/video/keyframe/collector.py \
  tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py
git commit -m "feat: select keyframes with siglip2 and ssim"
```

### Task 4: Realtime observer 接线、文档与模型资产操作入口

**Files:**
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Create: `scripts/export_siglip2_vision_onnx.py`
- Modify: `scripts/README.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py`
- Modify: `tests/tdd/realtime-video-as-of/test_realtime_video_as_of.py`

**Interfaces:**
- Consumes: Runtime `ProviderConfig`、Task 2 provider factory、Task 3 collector/selector config。
- Produces: `RealtimeVideoObserver(*, user_id: str, session_id: str, registry: ToolRegistry, memory_store: RealtimeVideoMemoryStore, provider_config: ProviderConfig | None = None, keyframe_root: Path | str = DEFAULT_KEYFRAME_ROOT, collector: AdaptiveKeyframeCollector | None = None, validator: ActionValidator | None = None, close_wait_seconds: float = DEFAULT_CLOSE_WAIT_SECONDS, clock_ns: Callable[[], int] = perf_counter_ns, wall_clock_ms: Callable[[], int] | None = None)` 和显式离线模型导出命令。

- [ ] **Step 1: 写 observer 接线和兼容语义的失败测试**

```python
def test_realtime_observer_uses_ten_second_siglip2_ssim_policy():
    config = ProviderConfig(
        provider_mode="real", vision_embedding_provider="local_siglip2",
        siglip2_vision_model_dir="/model-sentinel",
    )
    observer = RealtimeVideoObserver(
        user_id="u", session_id="s", registry=ToolRegistry(),
        memory_store=RealtimeVideoMemoryStore(), provider_config=config,
    )
    policy = observer.collector.selector.config
    assert policy.max_interval_seconds == 10.0
    assert policy.structural_weight == 0.4
    assert policy.semantic_weight == 0.6
```

扩展已有 realtime-as-of 测试，证明 `latest_keyframe_at_or_before()`、chat target sequence 和 10 秒 `LIVE_VIEW_SNAPSHOT_WAIT_SECONDS` 不因 collector 重接线改变。

- [ ] **Step 2: 运行两个临时 feature 并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/siglip2-keyframe tests/tdd/realtime-video-as-of
```

Expected: 新 observer policy 测试 FAIL；既有 as-of 测试继续 PASS。

- [ ] **Step 3: 接入 Runtime config 并增加离线导出脚本**

`_create_realtime_video_observer()` 把 `runtime.config` 传给 observer；observer 用配置构造 local provider、semantic detector、2 FPS collector 与 10 秒 selector。没有传 config 的测试构造保持 mock/offline。

导出脚本必须是 operator 显式入口，要求 `--model-id google/siglip2-base-patch16-224`、`--revision`、`--output-dir`，只导出 vision tower，并写包含预处理、revision、dimension、checksum 和 `embedding_space_id` 的 manifest。脚本启动时若缺少 export-only 依赖则明确退出，不在 Runtime 安装或下载任何内容。

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/export_siglip2_vision_onnx.py \
  --model-id google/siglip2-base-patch16-224 \
  --revision main \
  --output-dir .local/models/siglip2-base-patch16-224
```

脚本必须把 `main` 在下载时解析为响应中的精确 commit SHA，并把该 SHA 写入 manifest；Runtime 只认
manifest 中的精确 revision 和 checksum，不把 `main` 当作 embedding-space 身份。

权威文档将“像素差、SSIM、本地 embedding、最长 2 秒”更新为“SSIM + SigLIP2 image embedding、2 FPS semantic probe、最长 10 秒”，并注明 text tower 仅预留未启用。

- [ ] **Step 4: 运行两个 feature 并确认 GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/siglip2-keyframe tests/tdd/realtime-video-as-of
```

Expected: 全部 PASS，不访问网络或 GPU。

- [ ] **Step 5: 运行最小相邻回归和静态检查**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/live-view-tool-gating tests/tdd/siglip2-keyframe tests/tdd/realtime-video-as-of
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/media/video src/assistant_agent/api/agent_service_websocket.py \
  scripts/export_siglip2_vision_onnx.py
git diff --check
```

Expected: pytest 全绿、compileall 成功、`git diff --check` 无输出。

- [ ] **Step 6: 提交本任务文件**

```bash
git add src/assistant_agent/media/video/realtime_video_observer.py \
  src/assistant_agent/api/agent_service_websocket.py \
  scripts/export_siglip2_vision_onnx.py scripts/README.md \
  docs/media-agent-service-websocket.md \
  tests/tdd/siglip2-keyframe/test_siglip2_keyframe.py \
  tests/tdd/realtime-video-as-of/test_realtime_video_as_of.py
git commit -m "feat: wire siglip2 keyframes into realtime observer"
```

### Task 5: 本机 GPU 模型验证与完成审计

**Files:**
- Create only as ignored runtime artifacts: `.local/models/siglip2-base-patch16-224/**`
- No Git source changes unless validation exposes a deterministic defect, in which case return to RED/GREEN first。

**Interfaces:**
- Consumes: Task 4 export script and Runtime local provider。
- Produces: 本机模型 manifest、一次 GPU smoke 的结构化时延证据和需求逐项审计结果。

- [ ] **Step 1: 安装用户已允许的可选运行/导出依赖**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pip install -e ".[dev,local-vision-embedding]"
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pip install \
  "torch>=2.5,<3" "transformers>=4.53,<5" "optimum-onnx>=0.1,<1"
```

先用 pip dry-run/版本解析核对 CUDA wheel 与本机 Python 兼容；禁止修改系统 CUDA 或驱动。

- [ ] **Step 2: 固定官方模型 revision 并导出 vision ONNX**

从 Hugging Face 官方模型仓库解析一个 commit SHA，显式传入 Task 4 的脚本。确认产物目录位于 `.local/` 且 `git status --short` 不显示模型文件。

- [ ] **Step 3: 执行本机 GPU smoke 和时延测量**

使用一张合成或仓库生成的非用户 RGB 图片预热 5 次、测量 30 次，断言 provider 为 `local_siglip2`、dimension 与 manifest 一致、向量 L2 norm 约为 1、没有 text tokenizer/session。记录 P50/P95，但不提交图片、embedding 或 Provider raw output。

- [ ] **Step 4: 运行最终离线回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/live-view-tool-gating tests/tdd/siglip2-keyframe tests/tdd/realtime-video-as-of
```

Expected: PASS；真实模型资产存在也不能影响 mock/offline 测试。

- [ ] **Step 5: 逐项完成审计**

核对：SigLIP2 image tower 实际由 observer 使用；SSIM/semantic 独立触发；10 秒静态间隔；text tower 未加载；embedding-space 元数据可供未来 text tower；模型缺失 fail closed；A 时刻和工具暴露测试保持通过。只有所有证据均成立才报告完成。
