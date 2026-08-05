# 视觉上下文累积与压缩 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每个新语义关键帧在“当前单张图片 + 旧视觉摘要 + 最近逐关键帧文本”上完成 VLM 理解，并复用 AgentRuntime 的 token 预算状态机压缩较老视觉上下文，同时保持实时无积压和逐记录检索事实不变。

**Architecture:** 新增独立 `VisualContextService`，从 `SessionVisualSemanticStore` 读取不晚于当前帧的逐条视觉事实，用共享 `ContextWindowPolicy` 编译有界 `memory_context`。视觉 summary 与原始 record 分栏存放在同一个 session-owned store 生命周期中；`LLMVisualContextCompactor` 只替换成功覆盖的最老连续前缀，`visual_memory_search` 仍只检索原始 `VisualSemanticRecord`。

**Tech Stack:** Python 3.11、Pydantic v2、现有 `ChatAdapter`、`ContextWindowPolicy`、本地 `tokenizer.json`、asyncio、pytest（mock/offline）。

## Global Constraints

- SigLIP2 负责选出语义关键帧；VLM 不负责关键帧检测。
- 每次 VLM 仍只接收当前一张关键帧图片，历史仅以结构化文本上下文提供。
- 视觉预算默认复用 `target=0.40`、`trigger=0.70`、`hard=0.85`，但使用独立模型上限、tokenizer、safety margin、summary budget 和 store state。
- 压缩只覆盖最老连续前缀；压缩成功前不得替换 summary 或删除原始记录。
- `current_facts` 只能由当前图片支持；历史只允许生成 `changes` 和辅助不确定性判断。
- trigger 到 hard 之间压缩失败时保留原文并在预算内继续；hard 重试仍失败时跳过当前后台 VLM，不发送超预算 Provider 请求。
- VLM/压缩变慢时继续依赖 one-inflight、one-latest-pending 丢弃中间后台帧，不阻塞媒体 ACK 或主 Agent。
- `visual_memory_search` 不检索压缩摘要；`live_view_inspect` 默认仍读取最新逐帧记录；视觉 summary 不进入 conversation、主 Agent prompt 或 Mem0。
- mock/offline pytest 不读取真实 `.env`、不调用网络或真实 Provider；真实 Provider 验证不进入 pytest。
- Core invariant: unchanged。临时 RED/GREEN 只放入 `tests/tdd/visual-context-compaction/`，用户可手动整目录删除。

---

### Task 1: 建立视觉上下文模型和 session 状态边界

**Files:**
- Create: `src/assistant_agent/media/video/visual_context_models.py`
- Modify: `src/assistant_agent/media/video/semantic_store.py:28-488`
- Modify: `src/assistant_agent/media/video/__init__.py`
- Test: `tests/tdd/visual-context-compaction/test_visual_context_state.py`

**Interfaces:**
- Consumes: `VisualSemanticRecord`、`SessionVisualSemanticStore` 的既有 session/video 隔离与 retention 生命周期。
- Produces: `VisualContextSummary`、`VisualContextSnapshot`、`SessionVisualSemanticStore.records_for_context()`、`visual_context_snapshot()`、`replace_visual_context_summary()`。

- [ ] **Step 1: 写出视觉 summary 与原始 record 分离的失败测试**

```python
def test_visual_context_summary_does_not_replace_searchable_records(tmp_path: Path) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    second = store.record_success(_record(tmp_path, sequence=2, summary="杯子被拿起"))
    summary = VisualContextSummary(
        video_id="video-1",
        summary_revision=1,
        covered_record_ids=[first.record_id],
        first_sequence=1,
        last_sequence=1,
        stable_scene=["室内桌面"],
        object_last_confirmed=["杯子@1"],
        people_last_confirmed=[],
        changes=[],
        uncertainties=[],
        source_token_count=12,
        summary_token_count=5,
        compactor_model="fake-compactor",
    )

    store.replace_visual_context_summary("video-1", summary, expected_revision=0)

    assert store.records_for_context("video-1", before_sequence=3) == [first, second]
    assert store.search(_query_event(), video_id="video-1", limit=5)
    assert store.visual_context_snapshot("video-1").summary == summary
```

- [ ] **Step 2: 显式运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction/test_visual_context_state.py
```

Expected: FAIL，`VisualContextSummary` 或 store context API 尚不存在。

- [ ] **Step 3: 实现不可变视觉上下文模型**

在 `visual_context_models.py` 定义，确保 semantic store 只依赖纯模型文件，不与后续 service 形成循环导入：

```python
class VisualContextSummary(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: Literal["visual_context_summary_v1"] = "visual_context_summary_v1"
    video_id: str = Field(min_length=1, max_length=240)
    summary_revision: int = Field(ge=1)
    covered_record_ids: list[str] = Field(min_length=1)
    first_sequence: int = Field(ge=0)
    last_sequence: int = Field(ge=0)
    first_captured_at_ms: int | None = Field(default=None, ge=0)
    last_captured_at_ms: int | None = Field(default=None, ge=0)
    stable_scene: list[str] = Field(default_factory=list)
    object_last_confirmed: list[str] = Field(default_factory=list)
    people_last_confirmed: list[str] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    source_token_count: int = Field(ge=0)
    summary_token_count: int = Field(ge=0)
    compactor_model: str = Field(default="", max_length=240)

class VisualContextSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    video_id: str
    summary: VisualContextSummary | None = None
```

加入 model validator：覆盖 ID 非空且不重复；`first_sequence <= last_sequence`；时间范围若同时存在也必须有序；所有字符串去空白后非空并有界。

- [ ] **Step 4: 在 semantic store 中加入独立 summary 槽位和原子 CAS**

实现以下签名：

```python
def records_for_context(
    self,
    video_id: str,
    *,
    before_sequence: int,
) -> list[VisualSemanticRecord]:
    raise NotImplementedError

def visual_context_snapshot(self, video_id: str) -> VisualContextSnapshot:
    raise NotImplementedError

def replace_visual_context_summary(
    self,
    video_id: str,
    summary: VisualContextSummary,
    *,
    expected_revision: int,
) -> VisualContextSnapshot:
    raise NotImplementedError
```

`records_for_context` 只返回 `frame_sequence < before_sequence` 的防御性副本并按 sequence/created_at 排序。CAS 必须校验 video_id、revision 单调递增和 covered IDs 对应当时最老的连续未覆盖前缀；失败抛稳定错误 `visual_context_revision_conflict` 或 `visual_context_non_contiguous_prefix`。`clear()`、`close()` 与 pool eviction 同时清除 summary；record eviction 不由 compaction 触发。

- [ ] **Step 5: 补充生命周期与非法覆盖测试并确认 GREEN**

覆盖：future record 不进入上下文、跨 video summary 被拒绝、revision 冲突不修改旧 summary、非连续覆盖失败、`clear/close` 清除 summary 但普通 record retention 行为不变。

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction/test_visual_context_state.py \
  tests/tdd/unified-siglip2/test_visual_semantic_store.py
```

Expected: PASS。

- [ ] **Step 6: 提交 Task 1**

```bash
git add src/assistant_agent/media/video/visual_context_models.py \
  src/assistant_agent/media/video/semantic_store.py \
  src/assistant_agent/media/video/__init__.py \
  tests/tdd/visual-context-compaction/test_visual_context_state.py
git commit -m "feat: add session visual context state"
```

### Task 2: 用共享 ContextWindowPolicy 编译完整视觉请求预算

**Files:**
- Create: `src/assistant_agent/media/video/visual_context.py`
- Test: `tests/tdd/visual-context-compaction/test_visual_context_service.py`

**Interfaces:**
- Consumes: Task 1 的 store API；`assistant_agent.context.token_budget.ContextWindowPolicy`；具有 `count_text(str) -> int` 的本地 token counter。
- Produces: `VisualContextPack`、`VisualContextService.prepare(video_id, before_sequence, user_query)`、`VisualContextHardLimitError`、`VisualContextCompactor` protocol。

- [ ] **Step 1: 写出低预算、soft trigger 和 hard limit 的失败测试**

```python
def test_prepare_keeps_summary_and_recent_records_below_trigger(store) -> None:
    service = VisualContextService(
        store=store,
        token_counter=WordCounter(),
        window_policy=ContextWindowPolicy(
            input_token_limit=100,
            target_ratio=.40,
            trigger_ratio=.70,
            hard_ratio=.85,
            safety_margin_tokens=0,
            summary_max_tokens=20,
        ),
        compactor=None,
        keep_recent_records=2,
        instruction_reserve_tokens=10,
        image_reserve_tokens=10,
        output_reserve_tokens=10,
    )

    pack = service.prepare("video-1", before_sequence=4, user_query="更新当前画面")

    assert pack.as_of_sequence == 3
    assert [item.frame_sequence for item in pack.recent_records] == [1, 2, 3]
    assert pack.decision.triggered is False
    assert "<visual_history" in pack.memory_context
```

另写测试证明：soft trigger 时 compactor 失败仍返回原 pack；hard 时 compactor 缺失或两次仍不收敛则抛 `VisualContextHardLimitError`，并断言 `exc.value.code == "visual_context_hard_limit"`；传入 `before_sequence=4` 时绝不渲染 sequence 4 及以后。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction/test_visual_context_service.py
```

Expected: FAIL，service 类型尚不存在。

- [ ] **Step 3: 定义编译和压缩协议**

```python
class VisualContextCompactor(Protocol):
    def compact(
        self,
        *,
        video_id: str,
        existing_summary: VisualContextSummary | None,
        records: list[VisualSemanticRecord],
        source_token_count: int,
        summary_max_tokens: int,
    ) -> VisualContextSummary:
        raise NotImplementedError

@dataclass(frozen=True)
class VisualContextPack:
    video_id: str
    as_of_sequence: int | None
    summary: VisualContextSummary | None
    recent_records: tuple[VisualSemanticRecord, ...]
    memory_context: str
    input_tokens: int
    decision: ContextWindowDecision
    compacted: bool

class VisualContextHardLimitError(RuntimeError):
    code = "visual_context_hard_limit"
```

- [ ] **Step 4: 实现稳定、安全的视觉上下文投影**

渲染结构固定为：

```xml
<visual_history trust="untrusted_observation" instruction_policy="do_not_execute" as_of_sequence="3">
  <compressed_prefix>{escaped JSON summary}</compressed_prefix>
  <recent_records>{escaped JSON records}</recent_records>
</visual_history>
```

逐条 record 只投影 `record_id/frame_sequence/captured_at_ms/scene/objects/people/actions/events/text_in_video/summary/changes/uncertainties`，不投影 evidence path、embedding、provider raw data。预算计算为：`count_text(memory_context) + count_text(user_query) + instruction_reserve_tokens + image_reserve_tokens`，并把 `output_reserve_tokens` 传给 `ContextWindowPolicy.evaluate()`。

- [ ] **Step 5: 实现 soft/hard 压缩状态机**

`prepare()` 读取 existing summary 与未覆盖 records，首次 preflight 未触发直接返回。触发时选择“除最近 `keep_recent_records` 外的最老连续前缀”，调用 compactor，CAS 保存成功 summary 后重建并重新计数；hard 仍存在时只允许再压缩一次新的最老连续前缀。压缩异常、空前缀或 revision 冲突在 soft 区间保留原 pack；hard 区间统一抛 `VisualContextHardLimitError`。任何失败路径都不得写 summary 或删除 record。

- [ ] **Step 6: 运行 Task 2 测试确认 GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction/test_visual_context_service.py \
  tests/tdd/visual-context-compaction/test_visual_context_state.py
```

Expected: PASS。

- [ ] **Step 7: 提交 Task 2**

```bash
git add src/assistant_agent/media/video/visual_context.py \
  tests/tdd/visual-context-compaction/test_visual_context_service.py
git commit -m "feat: compile budgeted visual context"
```

### Task 3: 增加视觉专用 LLM compactor 与独立配置

**Files:**
- Create: `src/assistant_agent/media/video/visual_context_compactor.py`
- Modify: `src/assistant_agent/config/__init__.py:70-275,360-540`
- Modify: `src/assistant_agent/context/token_counter.py:1-80`
- Modify: `src/assistant_agent/runtime/runtime.py:250-305`
- Test: `tests/tdd/visual-context-compaction/test_visual_context_compactor.py`
- Test: `tests/tdd/visual-context-compaction/test_visual_context_config.py`

**Interfaces:**
- Consumes: Task 2 的 `VisualContextCompactor`；现有 `ChatAdapter`、`TokenizerJsonTokenCounter` 和 provider-mode 安全边界。
- Produces: `LLMVisualContextCompactor`、`create_visual_context_compactor()`、`create_visual_context_token_counter()`，以及 runtime 的 `visual_context_*` 依赖。

- [ ] **Step 1: 写出结构化输出校验与配置失败测试**

```python
def test_llm_visual_compactor_rejects_non_contiguous_coverage(records) -> None:
    adapter = ScriptedChatAdapter(response_text=json.dumps({
        "covered_record_ids": [records[1].record_id],
        "stable_scene": [],
        "object_last_confirmed": [],
        "people_last_confirmed": [],
        "changes": [],
        "uncertainties": [],
    }))
    compactor = LLMVisualContextCompactor(adapter, token_counter=WordCounter())

    with pytest.raises(VisualContextCompactionError, match="non_contiguous"):
        compactor.compact(
            video_id="video-1",
            existing_summary=None,
            records=records,
            source_token_count=30,
            summary_max_tokens=20,
        )
```

配置测试必须证明：`0 < target < trigger < hard <= 1`；`visual_context_compactor_mode=llm` 在 real mode 缺少视觉 tokenizer 时启动失败；mock mode 不创建真实 LLM compactor，也不联网加载 tokenizer。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction/test_visual_context_compactor.py \
  tests/tdd/visual-context-compaction/test_visual_context_config.py
```

Expected: FAIL，新 compactor/config 尚不存在。

- [ ] **Step 3: 添加独立视觉配置及本地 tokenizer factory**

在 `ProviderConfig` 增加并校验：

```python
visual_context_compactor_mode: ContextCompactorMode = "off"
visual_context_tokenizer_path: str | None = None
visual_context_input_token_limit: int = 32_768
visual_context_compaction_target_ratio: float = 0.40
visual_context_compaction_trigger_ratio: float = 0.70
visual_context_compaction_hard_ratio: float = 0.85
visual_context_compaction_safety_margin_tokens: int = 2_048
visual_context_summary_max_tokens: int = 2_048
visual_context_keep_recent_records: int = 4
visual_context_instruction_reserve_tokens: int = 1_024
visual_context_image_reserve_tokens: int = 2_048
visual_context_output_reserve_tokens: int = 2_048
```

环境变量统一使用 `REALTIME_VISUAL_CONTEXT_*` 前缀。`create_visual_context_token_counter()` 只在 `real + llm` 下从 `REALTIME_VISUAL_CONTEXT_TOKENIZER_PATH` 加载本地 tokenizer；缺失立即返回可解释配置错误，禁止联网下载或复用未经证明匹配 VLM 的 chat tokenizer。

- [ ] **Step 4: 实现 LLMVisualContextCompactor**

使用独立 system prompt，要求只返回 JSON object，字段固定为 `covered_record_ids/stable_scene/object_last_confirmed/people_last_confirmed/changes/uncertainties`。输入只包含 existing summary 和调用方已选定的连续 records；不得包含 evidence path 或 embedding。解析后由 `VisualContextSummaryValidator` 校验：覆盖 IDs 与输入严格相等且有序、video/sequence/time coverage 由代码计算而非信任模型、summary token 不超过上限、revision 为 existing+1。失败抛带安全信息的 `VisualContextCompactionError`，不做 deterministic fallback。

- [ ] **Step 5: 在 Runtime 创建依赖但不接入主 Agent context**

在 `AgentGraphRuntime.__init__` 创建：

```python
self.visual_context_token_counter = create_visual_context_token_counter(self.config)
self.visual_context_compactor = create_visual_context_compactor(
    self.config,
    self.chat_adapter,
    token_counter=self.visual_context_token_counter,
)
self.visual_context_window_policy = ContextWindowPolicy(
    input_token_limit=self.config.visual_context_input_token_limit,
    trigger_ratio=self.config.visual_context_compaction_trigger_ratio,
    target_ratio=self.config.visual_context_compaction_target_ratio,
    hard_ratio=self.config.visual_context_compaction_hard_ratio,
    safety_margin_tokens=self.config.visual_context_compaction_safety_margin_tokens,
    summary_max_tokens=self.config.visual_context_summary_max_tokens,
)
```

这些对象不得传给 `ContextService`、`AssistantContextPack` 或 conversation store；只供 realtime observer factory 注入。

- [ ] **Step 6: 运行配置、compactor 与现有 context 定向测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction/test_visual_context_compactor.py \
  tests/tdd/visual-context-compaction/test_visual_context_config.py \
  tests/core/integration/test_context_lifecycle.py
```

Expected: PASS；现有 `CTX-001` 行为不变。

- [ ] **Step 7: 提交 Task 3**

```bash
git add src/assistant_agent/media/video/visual_context_compactor.py \
  src/assistant_agent/config/__init__.py \
  src/assistant_agent/context/token_counter.py \
  src/assistant_agent/runtime/runtime.py \
  tests/tdd/visual-context-compaction/test_visual_context_compactor.py \
  tests/tdd/visual-context-compaction/test_visual_context_config.py
git commit -m "feat: add governed visual context compactor"
```

### Task 4: 强化 VLM 当前帧 grounding 与变化字段

**Files:**
- Modify: `src/assistant_agent/media/vision/models.py:70-125`
- Modify: `src/assistant_agent/providers/qwen_realtime_vision.py:25-65,491-498,610-635`
- Modify: `src/assistant_agent/media/video/video_adapter.py:10-115`
- Modify: `src/assistant_agent/media/video/semantic_store.py:28-72`
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py:587-640,943-980`
- Test: `tests/tdd/visual-context-compaction/test_visual_grounding_contract.py`

**Interfaces:**
- Consumes: Task 2 编译出的 `memory_context`。
- Produces: 在既有当前帧字段之外新增 `changes: list[str]` 与 `uncertainties: list[str]`；现有 `scene/objects/people/actions/events/text_in_video` 明确定义为当前帧事实。

- [ ] **Step 1: 写出 Provider schema 与搜索投影的失败测试**

```python
def test_qwen_result_separates_current_facts_from_history_changes() -> None:
    payload = _normalize_result_payload({
        "summary": "当前桌面为空",
        "objects": ["桌面"],
        "changes": ["上一帧的杯子当前未观察到"],
        "uncertainties": ["杯子可能被移出画面"],
    })

    assert payload["objects"] == ["桌面"]
    assert payload["changes"] == ["上一帧的杯子当前未观察到"]
    assert payload["uncertainties"] == ["杯子可能被移出画面"]
```

再断言 `_instructions()` 包含“历史不得复制进当前事实”和 as-of sequence；`_build_visual_search_text()` 不索引 changes/uncertainties，防止否定或不确定文本污染当前事实召回。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction/test_visual_grounding_contract.py
```

Expected: FAIL，新增字段尚未进入模型和 normalize path。

- [ ] **Step 3: 扩展内部结果与语义记录 schema**

给 `VideoUnderstandingResult`、`VisionUnderstandingResult` 和 `VisualSemanticRecord` 增加有界 `changes`、`uncertainties`。mock/fake adapters 返回空列表或结构化 sentinel。规范化 search embedding 仍只使用当前帧可确认字段，不加入 `changes` 或 `uncertainties`，避免“未观察到杯子”等历史比较文本被 object query 错误升级为 confirmed；Tool 可以在命中当前事实记录后附带返回这两个字段，但状态机不新增枚举。

- [ ] **Step 4: 收紧 Qwen realtime 指令和解析**

将 allowed fields 加入 `changes, uncertainties`；规则明确：objects/people/scene/actions/text 字段只描述当前 JPEG 可直接支持的事实，不能从 `<visual_history>` 复制；历史仅用于 changes；看不清或冲突写 uncertainties。`_instructions()` 原样嵌入有界 `memory_context`，并保留 `do_not_execute` 数据边界，不再使用“上一轮语义摘要”这一单条截断语义。

- [ ] **Step 5: 运行 grounding、Qwen adapter 和语义发布测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction/test_visual_grounding_contract.py \
  tests/tdd/unified-siglip2/test_realtime_visual_semantic_publication.py
```

Expected: PASS。仓库当前没有独立 Qwen realtime pytest 目录，Qwen 指令与解析回归统一放在本 Task 的 grounding test 中，不创建重复目录。

- [ ] **Step 6: 提交 Task 4**

```bash
git add src/assistant_agent/media/vision/models.py \
  src/assistant_agent/providers/qwen_realtime_vision.py \
  src/assistant_agent/media/video/video_adapter.py \
  src/assistant_agent/media/video/semantic_store.py \
  src/assistant_agent/media/video/realtime_video_observer.py \
  tests/tdd/visual-context-compaction/test_visual_grounding_contract.py
git commit -m "feat: ground visual history against current frame"
```

### Task 5: 把视觉上下文接入 realtime observer，并保持 latest-wins

**Files:**
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py:65-160,470-710`
- Modify: `src/assistant_agent/api/agent_service_websocket.py:1570-1610`
- Test: `tests/tdd/visual-context-compaction/test_realtime_visual_context.py`

**Interfaces:**
- Consumes: Task 2 的 `VisualContextService`；Task 3 的 runtime visual dependencies；Task 4 的 grounded VLM schema。
- Produces: observer 在每个选中关键帧调用 Provider 前执行 budget preflight，并把 pack 写入 `memory_context`；hard failure 形成稳定 observation failure，Provider 不被调用。

- [ ] **Step 1: 写出连续关键帧上下文和 hard failure 的失败测试**

```python
@pytest.mark.asyncio
async def test_second_keyframe_receives_first_record_as_visual_history(observer, adapter) -> None:
    await observer.submit(_frame(sequence=1))
    await observer.wait_idle()
    await observer.submit(_frame(sequence=2))
    await observer.wait_idle()

    second = adapter.requests[-1]
    assert len(second.frame_refs) == 1
    history = _decode_visual_history(second.memory_context)
    assert [item["frame_sequence"] for item in history["recent_records"]] == [1]

@pytest.mark.asyncio
async def test_hard_visual_context_failure_skips_provider(tmp_path: Path) -> None:
    adapter = CapturingVideoAdapter()
    context_service = AlwaysHardLimitVisualContextService()
    observer, semantic_store = _observer(
        tmp_path,
        adapter=adapter,
        visual_context_service=context_service,
    )

    await observer.promote(_frame(tmp_path, sequence=1))
    await observer.wait_idle()

    assert adapter.requests == []
    snapshot = semantic_store.snapshot("video-1")
    assert snapshot is not None
    assert snapshot.last_error["code"] == "visual_context_hard_limit"
    assert snapshot.pending_count == 0
    assert snapshot.in_flight is False
```

测试文件中的 `_decode_visual_history()` 使用 `html.unescape()` 和 `json.loads()` 解析 `<recent_records>` 内容，只断言结构化 sequence，不依赖完整自然语言；`AlwaysHardLimitVisualContextService.prepare()` 固定抛 `VisualContextHardLimitError`，用于证明 ToolExecutor/Provider 都未被调用。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction/test_realtime_visual_context.py
```

Expected: FAIL，observer 仍只传上一条 rolling summary。

- [ ] **Step 3: 向 observer 注入 VisualContextService**

构造器新增可选 `visual_context_service: VisualContextService | None`。生产 factory 使用当前 `semantic_lease.store`、runtime visual token counter/compactor/policy 和 config reserve 参数构造 service；测试可直接注入 fake service。未启用视觉 token counter 时保持当前有界 `memory_context` 兼容路径，并记录 unavailable，不得伪造已执行 token compaction。

- [ ] **Step 4: 在 Provider 调用前做 as-of preflight**

`_execute_observation()` 调用：

```python
pack = self.visual_context_service.prepare(
    video_id,
    before_sequence=item.sequence,
    user_query="更新当前场景、物体、人物、动作和重要变化。",
)
tool_input["memory_context"] = pack.memory_context
```

捕获 `VisualContextHardLimitError` 后直接返回失败 `ToolResult`，错误 code 固定 `visual_context_hard_limit`、`recoverable=True`；不要进入 `ToolExecutor.run_tool()`。现有 worker 会把失败写入 semantic snapshot、释放 inflight，并继续消费唯一 latest pending。

- [ ] **Step 5: 移除单条 `REALTIME_PREVIOUS_SUMMARY_MAX_CHARS` 主路径**

启用 service 时不再读取 `RealtimeVideoMemoryStore.current_state` 作为唯一历史；legacy memory store 只继续承担既有诊断/兼容 snapshot，不能成为新上下文事实源。关闭 visual compaction 配置时保留当前 2000 字符兼容行为，避免无 tokenizer 环境改变 mock/demo 行为。

- [ ] **Step 6: 运行 observer、as-of 与 latest-wins 回归**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction/test_realtime_visual_context.py \
  tests/tdd/unified-siglip2/test_realtime_visual_semantic_publication.py \
  tests/tdd/realtime-video-as-of/test_realtime_video_as_of.py \
  tests/tdd/unified-siglip2/test_semantic_frame_pipeline.py
```

Expected: PASS；Provider 每次仍只收到一张当前图片，pending 不超过 1，future record 不进入历史。

- [ ] **Step 7: 提交 Task 5**

```bash
git add src/assistant_agent/media/video/realtime_video_observer.py \
  src/assistant_agent/api/agent_service_websocket.py \
  tests/tdd/visual-context-compaction/test_realtime_visual_context.py
git commit -m "feat: apply visual context to realtime observations"
```

### Task 6: 补齐安全观测、权威文档与最终验证

**Files:**
- Modify: `src/assistant_agent/media/embedding/observability.py:20-100,250-315`
- Modify: `docs/context_engineering_status.md:90-140`
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/observability-harness.md`
- Modify: `tests/tdd/visual-context-compaction/test_visual_context_observability.py`

**Interfaces:**
- Consumes: Tasks 1-5 的状态与错误。
- Produces: content-free `visual_context.*` 事件、同步后的当前架构说明和可重复的最小验证证据。

- [ ] **Step 1: 写出观测脱敏失败测试**

```python
def test_visual_context_events_only_expose_budget_facts() -> None:
    observer = InMemoryEmbeddingObserver()
    emit_visual_context_observation(
        observer,
        "visual_context.compacted",
        session_id="secret-session",
        sequence=9,
        input_tokens=90,
        output_tokens=30,
        covered_count=4,
        status="succeeded",
    )

    payload = observer.events[-1].payload
    assert payload["session_id_digest"] != "secret-session"
    assert payload["covered_count"] == 4
    assert not ({"text", "summary", "query", "path", "vector"} & payload.keys())
```

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction/test_visual_context_observability.py
```

Expected: FAIL，event family 尚未登记。

- [ ] **Step 3: 实现 content-free 视觉上下文事件**

登记：`visual_context.preflight`、`visual_context.compacted`、`visual_context.compaction_failed`、`visual_context.hard_limit`。payload 只允许 session digest、sequence、input/effective/target token 数、usage ratio、covered/recent count、revision、latency 和枚举 status；不记录视觉全文、summary、query、record IDs、路径、向量或 Provider raw response。

- [ ] **Step 4: 同步当前权威文档**

文档明确写出：

```text
SigLIP2 keyframe
  -> current JPEG + VisualContextPack
  -> VLM current facts / changes / uncertainties
  -> VisualSemanticRecord
  -> raw-record search + next-call context projection
```

同时说明 target/trigger/hard 统一心智模型、独立 VLM tokenizer/limit、hard failure 跳过后台观察、summary 不进入主 Agent prompt/Mem0/search index，以及旧单条 rolling summary 只作为未启用 compaction 时的兼容路径。

- [ ] **Step 5: 运行 feature 最小显式集合**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/visual-context-compaction \
  tests/tdd/unified-siglip2 \
  tests/tdd/realtime-video-as-of
```

Expected: PASS。该命令不调用真实 Provider。

- [ ] **Step 6: 运行共享 context 与 runtime 核心安全网**

因为本实现直接复用 `ContextWindowPolicy` 并修改 runtime 依赖创建，运行已有相关 core 文件，不新增 core item：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/core/integration/test_context_lifecycle.py \
  tests/core/integration/test_runtime_lifecycle.py
```

Expected: PASS；`CTX-001`、`BOOT-001`、`RUN-001`、`LOOP-001` 仍保持原契约。

- [ ] **Step 7: 检查文档、diff 和工作区归属**

```bash
rg -n "visual context|视觉上下文|target|trigger|hard|Mem0|visual_memory_search" \
  docs/context_engineering_status.md \
  docs/multimodal-embedding-architecture.md \
  docs/media-agent-service-websocket.md \
  docs/observability-harness.md
git diff --check
git status --short
```

Expected: 无 whitespace error；只暂存本任务文件，现有 3D/language 等用户改动保持未暂存且不被回滚。

- [ ] **Step 8: 提交 Task 6**

```bash
git add src/assistant_agent/media/embedding/observability.py \
  docs/context_engineering_status.md \
  docs/multimodal-embedding-architecture.md \
  docs/media-agent-service-websocket.md \
  docs/observability-harness.md \
  tests/tdd/visual-context-compaction/test_visual_context_observability.py
git commit -m "docs: finalize visual context compaction architecture"
```

## 完成交付标准

- 第二个及后续关键帧的 VLM 请求包含不晚于当前帧的“旧摘要 + 最近逐条文本”，且只包含当前一张 JPEG。
- soft/hard 行为与 AgentRuntime `ContextWindowPolicy` 一致，视觉模型的绝对预算独立配置。
- 压缩成功才更新 summary；失败不删除或改写 `VisualSemanticRecord`。
- hard failure 不调用 Provider、不阻塞 ACK，observer 继续 one-inflight/one-latest-pending。
- `visual_memory_search` 的候选、排序和 as-of 仍来自逐条记录，不读取视觉 summary。
- 主 Agent prompt、conversation、Mem0 和 Tool catalog 不被动加入视觉上下文。
- Core invariant: unchanged。
- Tests: added `tests/tdd/visual-context-compaction` for temporary RED/GREEN; user may delete the directory manually。
