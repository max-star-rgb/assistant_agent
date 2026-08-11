# LangSmith 与 Langfuse 并行评测实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留现有 Langfuse 行为的同时，增加可选 LangSmith 日常 Trace、UI Dataset 沉淀和生产 Runtime Regression Experiment。

**Architecture:** canonical Runtime 仍只生成一套 OTel span spec；日常服务为 Langfuse 与 LangSmith 分别创建独立 observer/exporter。两个平台的 Experiment 使用各自专用 trace store；LangSmith adapter 通过 `Client.evaluate()` 调用同一个 `AgentGraphRuntime`，并把 Runtime 子树绑定到当前 LangSmith Experiment run。

**Tech Stack:** Python 3.12、Pydantic v2、OpenTelemetry OTLP/HTTP、Langfuse Python SDK 4.x、LangSmith Python SDK 0.8.x、pytest 8。

## Global Constraints

- Langfuse 的环境变量、exporter、Dataset、Experiment、Score、webhook 和 runtime audit 均不得删除、重命名或依赖 LangSmith。
- `ASSISTANT_AGENT_LANGSMITH_ENABLED=false` 是默认值；关闭时不得导入 LangSmith SDK、创建 client 或发送网络请求。
- LangSmith SDK 只加入 `eval` optional dependency，约束为 `langsmith>=0.8.11,<1`。
- 两个平台各自拥有同名固定 Dataset `assistant-agent-runtime-regressions`，但不自动同步。
- Dataset input、reference output 和 actual output 必须为 JSON object，禁止预序列化字符串。
- 真实回归必须同时满足 real Provider、`--allow-real-provider` 和 `--allow-runtime-side-effects`。
- 日常观测 fail-open；Experiment 配置、Dataset、Trace、Feedback 或完整性问题 fail-closed。
- LangSmith Experiment 的 Runtime spans 只发送到当前 LangSmith Experiment project，不能在 Langfuse 中形成孤儿子树。
- pytest 使用 mock/offline；临时测试只放入 `tests/tdd/langsmith-parallel-evaluation/`。
- 不使用端口 8089。

## 文件结构

新增：

- `src/assistant_agent/observability/langsmith_config.py`：LangSmith client/OTLP 配置。
- `src/assistant_agent/evaluation/langsmith_trace.py`：LangSmith RunTree 到 Runtime parent 的关联。
- `src/assistant_agent/evaluation/runtime_regression_contract.py`：双平台共享的数据契约。
- `evals/langsmith_runtime_regression/{__init__,experiment,cli}.py`：LangSmith 回归 adapter。
- `scripts/run_langsmith_runtime_regressions.py`：稳定入口。
- `tests/tdd/langsmith-parallel-evaluation/*.py`：独立 RED/GREEN 测试。

修改：

- `pyproject.toml`、`.env.example`
- `src/assistant_agent/observability/{otel_exporter,otel_mapping,trace_context,trace_persistence}.py`
- `src/assistant_agent/runtime/{event_publisher,runtime}.py`
- `src/assistant_agent/evaluation/experiment_runtime.py`
- `evals/runtime_regression/{experiment,cli}.py`
- `docs/observability-harness.md`、`evals/README.md`、`scripts/README.md`、`docs/authority.toml`

---

### Task 1: LangSmith 配置与可选依赖

**Files:**
- Create: `src/assistant_agent/observability/langsmith_config.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Test: `tests/tdd/langsmith-parallel-evaluation/test_langsmith_config.py`

**Interfaces:**
- Consumes: `OtlpHttpTextExporterConfig`。
- Produces: `LangSmithConfig.from_env(env, project_override=None)`、`LangSmithConfig.to_otlp_config()`、`create_langsmith_client_from_env(env=None)`。

- [ ] **Step 1: 写配置 RED 测试**

```python
def test_disabled_needs_no_sdk_or_credentials():
    assert LangSmithConfig.from_env({}).enabled is False

def test_enabled_builds_independent_otlp_config():
    config = LangSmithConfig.from_env({
        "ASSISTANT_AGENT_LANGSMITH_ENABLED": "true",
        "LANGSMITH_API_KEY": "test-key",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
        "LANGSMITH_PROJECT": "assistant-agent-runtime",
    })
    otlp = config.to_otlp_config()
    assert otlp.endpoint == "https://api.smith.langchain.com/otel/v1/traces"
    assert otlp.headers == {
        "x-api-key": "test-key",
        "Langsmith-Project": "assistant-agent-runtime",
    }

def test_enabled_rejects_missing_key():
    with pytest.raises(RuntimeError, match="LANGSMITH_API_KEY"):
        LangSmithConfig.from_env({"ASSISTANT_AGENT_LANGSMITH_ENABLED": "true"})
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_langsmith_config.py
```

Expected: FAIL，配置模块尚不存在。

- [ ] **Step 3: 实现配置与延迟 client factory**

```python
@dataclass(frozen=True)
class LangSmithConfig:
    enabled: bool = False
    api_key: str | None = None
    endpoint: str = "https://api.smith.langchain.com"
    project: str = "assistant-agent-runtime"
    workspace_id: str | None = None

    @classmethod
    def from_env(cls, env=None, *, project_override=None) -> "LangSmithConfig":
        values = os.environ if env is None else env
        enabled = values.get("ASSISTANT_AGENT_LANGSMITH_ENABLED", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        api_key = values.get("LANGSMITH_API_KEY") or None
        if enabled and api_key is None:
            raise RuntimeError("LANGSMITH_API_KEY is required when LangSmith is enabled")
        return cls(
            enabled=enabled,
            api_key=api_key,
            endpoint=values.get("LANGSMITH_ENDPOINT", cls.endpoint),
            project=project_override or values.get("LANGSMITH_PROJECT", cls.project),
            workspace_id=values.get("LANGSMITH_WORKSPACE_ID") or None,
        )

    def to_otlp_config(self) -> OtlpHttpTextExporterConfig:
        return OtlpHttpTextExporterConfig(
            enabled=self.enabled,
            endpoint=_otel_trace_endpoint(self.endpoint),
            headers={"x-api-key": self.api_key, "Langsmith-Project": self.project},
            service_name="assistant-agent-langsmith",
            include_content=self.enabled,
        )
```

`create_langsmith_client_from_env()` 只在 enabled 后局部 `from langsmith import Client`，并传入 `api_key`、`api_url`、可选 `workspace_id`。endpoint helper 同时支持 Cloud 和已含 `/api/v1` 的自托管 API，错误不得包含 key。

- [ ] **Step 4: 更新依赖与 env 示例**

```toml
eval = ["langfuse>=4.10,<5", "langsmith>=0.8.11,<1", "PyYAML>=6.0,<7"]
```

```text
ASSISTANT_AGENT_LANGSMITH_ENABLED=false
LANGSMITH_API_KEY=<set-in-local-shell>
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_PROJECT=assistant-agent-runtime
LANGSMITH_WORKSPACE_ID=
```

- [ ] **Step 5: 运行 GREEN 并提交**

Run: Task 1 Step 2。Expected: PASS。

```bash
git add pyproject.toml .env.example src/assistant_agent/observability/langsmith_config.py tests/tdd/langsmith-parallel-evaluation/test_langsmith_config.py
git commit -m "feat(observability): add optional LangSmith configuration"
```

### Task 2: LangSmith OTel 语义投影

**Files:**
- Modify: `src/assistant_agent/observability/otel_mapping.py`
- Test: `tests/tdd/langsmith-parallel-evaluation/test_langsmith_projection.py`

**Interfaces:**
- Consumes/Preserves: `build_text_otel_span_specs(events, conversation=None, memory_content=None, projection_context=None) -> list[OtelSpanSpec]`。
- Produces: 同一 spec 同时携带现有 `langfuse.*` 与新增 LangSmith 字段。

- [ ] **Step 1: 写投影 RED 测试**

构造含 Runtime、LLM、Tool 和 terminal summary 的 events：

```python
root = next(span for span in spans if span.name == "agent.runtime")
llm = next(span for span in spans if span.name == "llm.chat")
tool = next(span for span in spans if span.name == "tool.execute")
assert root.attributes["langfuse.trace.name"] == "assistant.turn"
assert root.attributes["langsmith.trace.name"] == "assistant.turn"
assert root.attributes["langsmith.span.kind"] == "chain"
assert json.loads(root.attributes["inputs"])["role"] == "user"
assert json.loads(root.attributes["outputs"])["role"] == "assistant"
assert llm.attributes["langsmith.span.kind"] == "llm"
assert tool.attributes["langsmith.span.kind"] == "tool"
assert root.attributes["langsmith.trace.session_id"] == "session-1"
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_langsmith_projection.py
```

Expected: FAIL，缺少 `langsmith.trace.name`。

- [ ] **Step 3: 增加纯映射 helper**

```python
def _langsmith_span_kind(event: TraceEvent | None, *, root: bool = False) -> str:
    if root:
        return "chain"
    if _observation_type(event) == "generation":
        return "llm"
    if event is not None and _event_name(event) in {"tool.finished", "tool.failed"}:
        return "tool"
    return "chain"

def _langsmith_io(input_value: str, output_value: str) -> dict[str, str]:
    return {"inputs": input_value, "outputs": output_value}
```

在 trace/root/event I/O helper 中增加 `langsmith.trace.name`、`langsmith.span.kind`、`langsmith.trace.session_id`、`langsmith.metadata.*`、`inputs` 和 `outputs`。现有 `langfuse.*` 赋值原样保留，只复制通过既有 allowlist 的内容。

- [ ] **Step 4: 运行 GREEN 与 Langfuse 投影回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_langsmith_projection.py tests/tdd/workflow-langfuse-overview/test_workflow_observability.py tests/tdd/vlm-trace-correlation-content/test_otel_span_hierarchy.py
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/observability/otel_mapping.py tests/tdd/langsmith-parallel-evaluation/test_langsmith_projection.py
git commit -m "feat(observability): project LangSmith OTel semantics"
```

### Task 3: 日常双 exporter 与 Experiment 后端隔离

**Files:**
- Modify: `src/assistant_agent/observability/otel_exporter.py`
- Modify: `src/assistant_agent/observability/trace_persistence.py`
- Test: `tests/tdd/langsmith-parallel-evaluation/test_dual_trace_export.py`

**Interfaces:**
- Produces: `create_text_otel_trace_observer(config)`、`create_langsmith_text_otel_trace_observer_from_env(env=None, project_override=None, required=False)`、`create_langsmith_experiment_trace_store(project_id)`。
- Preserves: `create_text_otel_trace_observer_from_env()` 和 `create_experiment_trace_store()` 的 Langfuse 行为。

- [ ] **Step 1: 写装配与隔离 RED 测试**

```python
def test_server_store_registers_both_observers(monkeypatch, tmp_path):
    monkeypatch.setattr(persistence, "create_text_otel_trace_observer_from_env", lambda: "langfuse")
    monkeypatch.setattr(persistence, "create_langsmith_text_otel_trace_observer_from_env", lambda: "langsmith")
    store = persistence.create_server_trace_store(path=tmp_path)
    assert _observer_labels(store) == ["langfuse", "langsmith"]

def test_one_observer_failure_does_not_block_the_other():
    manager = HookManager([FailingObserver(), RecordingObserver()])
    manager.on_trace_event(_terminal_event())
    assert recording.count == 1

def test_langsmith_experiment_store_excludes_langfuse(monkeypatch):
    persistence.create_langsmith_experiment_trace_store(project_id="experiment-id")
    assert langsmith_projects == ["experiment-id"]
    assert langfuse_calls == 0
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_dual_trace_export.py
```

Expected: FAIL，LangSmith factory 尚不存在。

- [ ] **Step 3: 抽出显式 config observer factory**

```python
def create_text_otel_trace_observer(config: OtlpHttpTextExporterConfig):
    setup = create_otlp_http_text_span_exporter(config)
    if setup.status != "ready" or setup.exporter is None:
        return None
    return TextOtelTraceObserver(
        BufferedTextOtelSpanExporter(setup.exporter, capacity=config.queue_capacity),
        enabled=True,
        include_content=config.include_content,
        include_vlm_input_content=config.include_vlm_input_content,
        include_memory_content=config.include_memory_content,
    )
```

现有 env factory 调用该 helper。LangSmith factory 使用 `LangSmithConfig.to_otlp_config()`；`required=True` 时 disabled/unavailable 抛出安全 `RuntimeError`。

- [ ] **Step 4: 装配双 daily observer 和两个专用 Experiment store**

`create_server_trace_store()` 分别追加两个 `HookTraceStore`。新增 `create_langfuse_experiment_trace_store()`；保留 `create_experiment_trace_store()` 为其兼容别名。`create_langsmith_experiment_trace_store(project_id: str)` 只装配 project override 的 LangSmith observer，不装配 Langfuse 或日常 Score observer。

- [ ] **Step 5: 运行 GREEN 与持久化回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_dual_trace_export.py tests/tdd/trace-ledger-retention/test_trace_ledger_retention.py tests/tdd/runtime-eval-feedback-loop/test_experiment_runtime_host.py
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/observability/otel_exporter.py src/assistant_agent/observability/trace_persistence.py tests/tdd/langsmith-parallel-evaluation/test_dual_trace_export.py
git commit -m "feat(observability): export daily traces to LangSmith in parallel"
```

### Task 4: 共享 Runtime Regression 数据契约

**Files:**
- Create: `src/assistant_agent/evaluation/runtime_regression_contract.py`
- Modify: `evals/runtime_regression/experiment.py`
- Test: `tests/tdd/langsmith-parallel-evaluation/test_runtime_regression_contract.py`
- Test: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_experiment.py`

**Interfaces:**
- Produces: `request_text(item_id, inputs)`、`validate_failure_baseline(item_id, reference_outputs)`、`assistant_output(state)`。
- Consumes: 两个平台 adapter 提供的 object，不在共享层读取 SDK model。

- [ ] **Step 1: 写契约 RED 测试**

```python
def test_contract_preserves_object_shape():
    assert request_text("item-1", {
        "role": "user", "content": "问题", "truncated": False,
    }) == "问题"
    baseline = validate_failure_baseline("item-1", {
        "role": "assistant", "content": "失败回答", "chars": 4,
        "truncated": False, "terminal_status": "completed",
    })
    assert baseline["content"] == "失败回答"

@pytest.mark.parametrize("value", [None, "{\"role\":\"assistant\"}"])
def test_contract_rejects_non_object_baseline(value):
    with pytest.raises(RuntimeError, match="must be an object"):
        validate_failure_baseline("item-1", value)
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_runtime_regression_contract.py
```

Expected: FAIL，共享模块尚不存在。

- [ ] **Step 3: 实现纯契约并迁移 Langfuse adapter**

```python
def request_text(item_id: str, inputs: Mapping[str, Any]) -> str:
    if inputs.get("truncated") is True:
        raise RuntimeError(f"runtime regression item {item_id!r} input is truncated")
    if inputs.get("role") not in (None, "user"):
        raise RuntimeError(f"runtime regression item {item_id!r} input role must be user")
    text = inputs.get("content", inputs.get("request"))
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(f"runtime regression item {item_id!r} has no user content")
    return text
```

`validate_failure_baseline()` 要求 dict、`role=assistant`、非空 content；`assistant_output()` 返回 `{role, content, chars, truncated, terminal_status}`。Langfuse experiment 改为导入这些函数，公开 API、Score 和 trace 完整性逻辑不变。

- [ ] **Step 4: 运行 GREEN 和完整 Langfuse TDD**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_runtime_regression_contract.py tests/tdd/runtime-eval-feedback-loop
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/evaluation/runtime_regression_contract.py evals/runtime_regression/experiment.py tests/tdd/langsmith-parallel-evaluation/test_runtime_regression_contract.py tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_experiment.py
git commit -m "refactor(eval): share runtime regression data contract"
```

### Task 5: LangSmith Experiment 与 Runtime Trace 关联

**Files:**
- Create: `src/assistant_agent/evaluation/langsmith_trace.py`
- Modify: `src/assistant_agent/observability/trace_context.py`
- Modify: `src/assistant_agent/runtime/event_publisher.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/observability/otel_mapping.py`
- Modify: `src/assistant_agent/evaluation/experiment_runtime.py`
- Test: `tests/tdd/langsmith-parallel-evaluation/test_langsmith_experiment_runtime.py`
- Test: `tests/tdd/runtime-eval-feedback-loop/test_experiment_runtime_host.py`

**Interfaces:**
- Produces: `RuntimeExperimentTraceLink`、`LangSmithExperimentBinding`、`current_langsmith_experiment_binding()`。
- Extends: `create_experiment_runtime_host(runtime_builder, trace_store_factory=None, trace_context_provider=current_runtime_trace_context)`，默认仍使用 Langfuse OTel current span。

- [ ] **Step 1: 写 RunTree 关联 RED 测试**

```python
run_tree = SimpleNamespace(
    id=UUID("11111111-2222-3333-4444-555555555555"),
    trace_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
    session_id=UUID("99999999-8888-7777-6666-555555555555"),
    reference_example_id=UUID("01234567-89ab-cdef-0123-456789abcdef"),
)
binding = current_langsmith_experiment_binding()
assert binding.project_id == str(run_tree.session_id)
assert binding.trace_context.trace_id == run_tree.trace_id.hex
assert binding.trace_context.parent_span_id == run_tree.id.bytes[:8].hex()
assert binding.trace_context.experiment_link.parent_run_id == str(run_tree.id)
```

同时断言 Runtime root attributes：

```python
assert root.attributes["langsmith.trace.id"] == str(run_tree.trace_id)
assert root.attributes["langsmith.span.parent_id"] == str(run_tree.id)
assert root.attributes["langsmith.trace.session_id"] == str(run_tree.session_id)
assert root.attributes["langsmith.reference_example_id"] == str(run_tree.reference_example_id)
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_langsmith_experiment_runtime.py
```

Expected: FAIL，binding/model 尚不存在。

- [ ] **Step 3: 增加受校验的外部关联 model**

```python
class RuntimeExperimentTraceLink(BaseModel):
    model_config = ConfigDict(frozen=True)
    backend: Literal["langsmith"]
    trace_id: str = Field(min_length=1, max_length=64)
    parent_run_id: str = Field(min_length=1, max_length=64)
    experiment_id: str = Field(min_length=1, max_length=64)
    reference_example_id: str = Field(min_length=1, max_length=64)

class RuntimeTraceContext(BaseModel):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    experiment_link: RuntimeExperimentTraceLink | None = None
```

`RunStartedFact` 增加 `experiment_trace_link`；Runtime 从 `trace_context` 传入。publisher 只写固定 `evaluation_backend/trace_id/parent_run_id/experiment_id/reference_example_id`，不接收任意 request metadata。

- [ ] **Step 4: 实现 RunTree adapter 与可注入 host**

```python
@dataclass(frozen=True)
class LangSmithExperimentBinding:
    project_id: str
    trace_context: RuntimeTraceContext

def current_langsmith_experiment_binding() -> LangSmithExperimentBinding | None:
    run = _current_run_tree()
    if run is None or run.session_id is None or run.reference_example_id is None:
        return None
    return LangSmithExperimentBinding(
        project_id=str(run.session_id),
        trace_context=RuntimeTraceContext(
            trace_id=run.trace_id.hex,
            parent_span_id=run.id.bytes[:8].hex(),
            experiment_link=RuntimeExperimentTraceLink(
                backend="langsmith",
                trace_id=str(run.trace_id),
                parent_run_id=str(run.id),
                experiment_id=str(run.session_id),
                reference_example_id=str(run.reference_example_id),
            ),
        ),
    )
```

`ExperimentRuntimeHost` 保存 `trace_context_provider: Callable[[], RuntimeTraceContext | None]`；默认 provider 仍为 `current_runtime_trace_context`。LangSmith target 注入返回固定 binding context 的 provider。

- [ ] **Step 5: 投影官方 LangSmith 关联字段**

`otel_mapping.py` 只从 `run.started` 的固定 evaluation attributes 构造 `langsmith.trace.id`、`langsmith.span.parent_id`、`langsmith.trace.session_id`、`langsmith.reference_example_id`，且只放在 Runtime root。

- [ ] **Step 6: 运行 GREEN 和 Langfuse host 回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_langsmith_experiment_runtime.py tests/tdd/runtime-eval-feedback-loop/test_experiment_runtime_host.py
```

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/assistant_agent/evaluation/langsmith_trace.py src/assistant_agent/evaluation/experiment_runtime.py src/assistant_agent/observability/trace_context.py src/assistant_agent/observability/otel_mapping.py src/assistant_agent/runtime/event_publisher.py src/assistant_agent/runtime/runtime.py tests/tdd/langsmith-parallel-evaluation/test_langsmith_experiment_runtime.py tests/tdd/runtime-eval-feedback-loop/test_experiment_runtime_host.py
git commit -m "feat(eval): bind runtime traces to LangSmith experiments"
```

### Task 6: LangSmith Dataset 与原生 Experiment adapter

**Files:**
- Create: `evals/langsmith_runtime_regression/__init__.py`
- Create: `evals/langsmith_runtime_regression/experiment.py`
- Test: `tests/tdd/langsmith-parallel-evaluation/test_langsmith_runtime_regression.py`

**Interfaces:**
- Consumes: `Client.read_dataset/list_examples/evaluate/list_runs/list_feedback`、shared contract、`LangSmithExperimentBinding`。
- Produces: `inspect_langsmith_runtime_regression_dataset()`、`run_langsmith_runtime_regression_experiment()`、`wait_for_langsmith_runtime_regression_completeness()`。

- [ ] **Step 1: 写 Dataset 与 target RED 测试**

Fake Example 使用 object：

```python
example = SimpleNamespace(
    id=UUID("01234567-89ab-cdef-0123-456789abcdef"),
    inputs={"role": "user", "content": "重跑问题", "chars": 4, "truncated": False},
    outputs={"role": "assistant", "content": "原始失败回答", "chars": 6,
             "truncated": False, "terminal_status": "completed"},
    metadata={"active": True, "source_trace_id": "source-trace"},
)
```

断言 `evaluate()` 收到 active Example iterator、`evaluators=[]`、`blocking=True` 和 concurrency；target 调用 Runtime 并返回 canonical assistant object。另写用例拒绝字符串 outputs、空 content、truncated input 和没有 active Example。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_langsmith_runtime_regression.py
```

Expected: FAIL，adapter 尚不存在。

- [ ] **Step 3: 实现 inspect 和 target**

```python
@dataclass(frozen=True)
class LangSmithRuntimeRegressionSettings:
    model: str
    runtime_factory: Callable[[LangSmithExperimentBinding], RuntimeRegressionRuntime]
    run_name: str
    git_commit: str
    max_concurrency: int = 1

def target(inputs: dict[str, Any]) -> dict[str, Any]:
    binding = current_langsmith_experiment_binding()
    if binding is None:
        raise RuntimeError("LangSmith Experiment target has no active RunTree binding")
    runtime = settings.runtime_factory(binding)
    try:
        state = runtime.run_state(UserRequest(
            user_id="runtime-regression",
            session_id=f"runtime-regression-{binding.trace_context.experiment_link.reference_example_id}",
            text=request_text(
                binding.trace_context.experiment_link.reference_example_id,
                inputs,
            ),
            metadata={
                "runtime_regression": {
                    "dataset_item_id": binding.trace_context.experiment_link.reference_example_id,
                    "backend": "langsmith",
                }
            },
        ))
        return assistant_output(state)
    finally:
        runtime.close()
```

调用 `client.evaluate(target, data=active_examples, evaluators=[], experiment_prefix=settings.run_name, blocking=True, error_handling="log", metadata={evaluation_mode, model, git_commit})` 并 materialize rows。result 保存 experiment id/name/url、dataset id、example ids 和 run ids；映射不完整时 fail closed。

- [ ] **Step 4: 写完整性与 Feedback RED 测试**

每个 active Example 必须恰好对应一个 root run；其 root input/output 为非空 dict，trace 子树必须含 `agent.runtime` 和 `llm.chat`。`list_feedback(run_ids=run_ids)` 必须包含：

```python
REQUIRED_LANGSMITH_FEEDBACK_KEYS = (
    "assistant_agent.quality.response_quality.experiment",
    "assistant_agent.quality.grounding.experiment",
    "assistant_agent.quality.regression_improvement.experiment",
)
```

第一轮缺失、第二轮完整应成功；timeout 异常列出 example id 和缺少 key。

- [ ] **Step 5: 实现轮询并运行 GREEN**

```python
def wait_for_langsmith_runtime_regression_completeness(
    client: Any, *, experiment_id: str, example_ids: tuple[str, ...],
    timeout_seconds: float = 180.0, poll_interval_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> LangSmithCompletenessResult:
    deadline = monotonic() + timeout_seconds
    while True:
        roots = list(client.list_runs(project_id=experiment_id, is_root=True))
        audit = _audit_roots_traces_and_feedback(client, roots, example_ids)
        if audit.complete:
            return audit
        if monotonic() >= deadline:
            raise RuntimeError(f"LangSmith Experiment incomplete: {audit.problems!r}")
        sleep(poll_interval_seconds)
```

Run: Task 6 Step 2。Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add evals/langsmith_runtime_regression tests/tdd/langsmith-parallel-evaluation/test_langsmith_runtime_regression.py
git commit -m "feat(eval): add LangSmith runtime regression experiment"
```

### Task 7: LangSmith Runtime Regression CLI

**Files:**
- Create: `evals/langsmith_runtime_regression/cli.py`
- Create: `scripts/run_langsmith_runtime_regressions.py`
- Modify: `evals/runtime_regression/cli.py`
- Test: `tests/tdd/langsmith-parallel-evaluation/test_langsmith_runtime_regression_cli.py`
- Test: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_cli.py`

**Interfaces:**
- Consumes: client factory、LangSmith Experiment store、binding 和 Experiment adapter。
- Produces: `main(argv: Sequence[str] | None = None) -> int` 与稳定脚本入口。

- [ ] **Step 1: 写 CLI RED 测试**

```python
def test_inspect_never_builds_provider_or_runtime(monkeypatch):
    monkeypatch.setattr(cli, "_langsmith_client", lambda: FakeClient())
    monkeypatch.setattr(cli.ProviderConfig, "from_env", lambda: (_ for _ in ()).throw(AssertionError()))
    assert cli.main(["--inspect", "--no-env-file"]) == 0

def test_preflight_requires_both_operator_flags():
    with pytest.raises(SystemExit):
        cli.main(["--preflight", "--no-env-file", "--allow-real-provider"])

def test_run_requires_real_provider(monkeypatch):
    monkeypatch.setattr(cli.ProviderConfig, "from_env", lambda: SimpleNamespace(provider_mode="mock"))
    assert cli.main(["--run", "--run-name", "r1", "--no-env-file",
                     "--allow-real-provider", "--allow-runtime-side-effects"]) == 2

def test_run_uses_langsmith_only_experiment_store(monkeypatch):
    binding = _binding("experiment-id", "example-id")
    runtime = cli._create_item_runtime(_real_config(), binding)
    assert captured_store_project == "experiment-id"
    assert captured_langfuse_store_calls == 0

def test_run_returns_two_when_feedback_or_trace_is_missing(monkeypatch):
    monkeypatch.setattr(cli, "wait_for_langsmith_runtime_regression_completeness",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("missing feedback")))
    assert _run_cli_with_fakes(monkeypatch) == 2
```

成功 JSON 契约：

```python
{
    "action": "run",
    "backend": "langsmith",
    "dataset_name": "assistant-agent-runtime-regressions",
    "experiment_id": "experiment-id",
    "experiment_name": "run-name",
    "experiment_url": "https://smith.invalid/experiment",
    "example_ids": ["example-id"],
    "run_ids": ["run-id"],
    "feedback": {"example-id": {"score-key": True}},
}
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_langsmith_runtime_regression_cli.py
```

Expected: FAIL，CLI 尚不存在。

- [ ] **Step 3: 实现 inspect/preflight/run**

参数为 `--inspect`、`--preflight`、`--run`、`--run-name`、`--max-concurrency`、`--feedback-wait-timeout-seconds`、两个 allow flag、`--env-file` 和 `--no-env-file`。

```python
def _create_item_runtime(config, binding):
    return create_experiment_runtime_host(
        lambda trace_store: AgentGraphRuntime(config=config, trace_store=trace_store),
        trace_store_factory=lambda: create_langsmith_experiment_trace_store(
            project_id=binding.project_id,
        ),
        trace_context_provider=lambda: binding.trace_context,
    )
```

`--inspect` 只读 Dataset；`--preflight` 验证 Dataset、Provider config、flags 和 exporter readiness；`--run` 才调用 Runtime。`finally` 依次执行 `client.flush()` 和 `client.close()`。基础设施失败输出 `langsmith_runtime_regression_infrastructure_failure` 并返回 2。

- [ ] **Step 4: 固定 Langfuse CLI 的专用 store**

`evals/runtime_regression/cli.py::_create_item_runtime()` 显式注入 `create_langfuse_experiment_trace_store`，避免 LangSmith 装配改变 Langfuse Experiment。保持原输出和入口不变。

- [ ] **Step 5: 运行 GREEN 和 Langfuse CLI 回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_langsmith_runtime_regression_cli.py tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_cli.py
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add evals/langsmith_runtime_regression/cli.py scripts/run_langsmith_runtime_regressions.py evals/runtime_regression/cli.py tests/tdd/langsmith-parallel-evaluation/test_langsmith_runtime_regression_cli.py tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_cli.py
git commit -m "feat(eval): add controlled LangSmith regression runner"
```

### Task 8: 权威文档与导航同步

**Files:**
- Modify: `docs/observability-harness.md`
- Modify: `evals/README.md`
- Modify: `scripts/README.md`
- Modify: `docs/authority.toml`

**Interfaces:**
- Documents: 双写开关、内容边界、UI Dataset 操作、三个 Feedback key、CLI、安全门槛和事实源隔离。

- [ ] **Step 1: 更新 observability authority**

写明 canonical span 可投影至两个独立 exporter；LangSmith 默认关闭；显式启用意味着 operator 允许符合现有清洗策略的用户/助手内容发送至配置 endpoint；任一 exporter 失败不改变业务结果或另一后端。

- [ ] **Step 2: 更新 eval authority**

写明 `Tracing Projects → Add to Annotation Queue/Dataset`、固定 Dataset object schema、绑定三个 canonical evaluator key、新 runner 的 inspect/preflight/run，以及两个平台 Dataset 不同步。

- [ ] **Step 3: 更新脚本导航和 manifest**

把新 script、eval package、LangSmith adapter/config 和 TDD 路径加入对应 `source_globs` 与 verification；不新增重复 authority domain。

- [ ] **Step 4: 验证并提交**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
```

Expected: PASS。

```bash
git add docs/observability-harness.md evals/README.md scripts/README.md docs/authority.toml
git commit -m "docs: document parallel LangSmith evaluation"
```

### Task 9: 完整验证与交付审核

**Files:**
- Verify only；只在证据揭示本任务缺陷时修改 task-owned files。

**Interfaces:**
- Proves: 关闭零影响、日常双写、失败隔离、object schema、生产 Runtime 复用、Experiment 完整性、Langfuse 无回归和无隐式真实调用。

- [ ] **Step 1: 运行 LangSmith feature TDD**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langsmith-parallel-evaluation
```

Expected: PASS。

- [ ] **Step 2: 运行 Langfuse Runtime Regression TDD**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime-eval-feedback-loop
```

Expected: PASS。

- [ ] **Step 3: 运行观测定向回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/runtime_audit tests/tdd/workflow-langfuse-overview tests/tdd/vlm-trace-correlation-content tests/tdd/trace-ledger-retention
```

Expected: PASS。无关的既有失败只记录证据，不修改无关模块。

- [ ] **Step 4: 运行静态与文档检查**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent evals/langsmith_runtime_regression scripts/run_langsmith_runtime_regressions.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
git diff --check
```

Expected: 全部退出 0。

- [ ] **Step 5: 执行 mutation checks**

用 `apply_patch` 临时移除 daily LangSmith observer，确认 `test_dual_trace_export.py` 失败后恢复；再临时移除 Runtime root 的 `langsmith.span.parent_id`，确认 `test_langsmith_experiment_runtime.py` 失败后恢复。重跑 Task 9 Step 1，临时改动不得提交。

- [ ] **Step 6: 逐项完成审计**

```text
[ ] LangSmith disabled 时无 client/import/network，Langfuse tests green
[ ] 两个 daily observer 均接收同一 canonical trace
[ ] 任一 observer/exporter 失败不阻止另一后端
[ ] Dataset inputs/reference_outputs 保持 dict
[ ] target 复用 AgentGraphRuntime
[ ] root input/output + agent.runtime + llm.chat 完整
[ ] 三个 Feedback key 完整，否则命令非零
[ ] pytest 未调用真实 Provider
[ ] authority validator green
```

- [ ] **Step 7: 请求并处理代码审查**

使用 `superpowers:requesting-code-review` 检查规格覆盖、Langfuse 回归、凭据泄露、父子 identity、并发安全和关闭生命周期。有效发现按 `superpowers:receiving-code-review` 复现后修复，再运行最小相关测试。

- [ ] **Step 8: 检查提交范围**

```bash
git status --short
git log --oneline --decorate -12
git diff --check
```

只提交本任务文件，不提交 `.env`、真实 Trace、Experiment 结果、`.data/evals/` 或用户现有改动。

## 真实验收（需再次明确授权）

mock 实施不自动执行真实调用。用户提供 LangSmith 凭据并明确授权后，只选择一个人工 Example：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_langsmith_runtime_regressions.py --inspect
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_langsmith_runtime_regressions.py --preflight --allow-real-provider --allow-runtime-side-effects
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_langsmith_runtime_regressions.py --run --run-name assistant-agent-langsmith-first-run --max-concurrency 1 --allow-real-provider --allow-runtime-side-effects
```

验收 UI 中 input/reference output/actual output 为对象、Runtime/LLM/Tool 父子树完整、三个 Feedback 已落库；随后运行现有 Langfuse `--inspect` 证明其入口仍独立。最终报告真实 Provider 调用范围、模型、Experiment ID/URL、Example ID、Trace ID 和 Feedback。
