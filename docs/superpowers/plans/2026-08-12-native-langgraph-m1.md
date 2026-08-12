# 原生 LangGraph M1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可复用的 `AssistantTurnGraph` 原生执行基线，使生产 Runtime 使用稳定 compiled graph、LangGraph runtime context 和原生 async stream，并让 LangSmith 直接形成 graph/node/LLM/tool 执行树和运行最小 Experiment。

**Architecture:** `AssistantTurnGraphApp` 持有一次编译的 `CompiledStateGraph`，每次运行通过 `GraphRuntimeContext` 注入非持久依赖，通过 `GraphExecutionIdentity` 提供稳定 thread 与 turn namespace。同步兼容入口和 Agent-Service 产品事件保持不变，但主异步路径直接消费 `astream(version="v2")`；LangSmith 继承当前 Experiment RunTree 或显式开启项目 tracing，不再通过自研 OTel 重建 LangSmith Runtime 子树。

**Tech Stack:** Python 3.11、LangGraph 1.2.4、langgraph-checkpoint 4.1.1、langchain-core 1.4.3、LangSmith 0.10.18、Pydantic、asyncio、pytest。

## Global Constraints

- 通用 Graph Runtime 职责优先使用 LangGraph 原生能力，不继续扩展自研替代实现。
- M1 不实现持久 checkpointer、`interrupt`/resume、Workflow v2 DAG 迁移或 Langfuse 全量删除；它们属于后续里程碑。
- Agent-Service 强依赖协议、媒体 API、产品事件、取消和终态语义保持兼容。
- Tool 调用仍经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- pytest 使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`、local/offline，不读取真实 `.env` 或调用远端。
- LangSmith 是唯一新增 trace/eval 能力目标；Langfuse 在 M1 只保持兼容。
- Graph state 不得保存 client、executor/registry、数据库连接、event sink、callback、cancel token、媒体或 artifact 正文。
- 只提交当前 task 涉及文件，不回滚用户改动。

---

## 文件结构

**新建：**

- `src/assistant_agent/runtime/assistant_graph_app.py`：稳定 compiled graph、身份和 v2 stream consumer。
- `src/assistant_agent/observability/langsmith_native.py`：原生 tracing 上下文及安全 LLM/Tool 投影。
- `tests/tdd/native-langgraph-runtime/test_graph_app.py`：compiled graph、context、identity、stream。
- `tests/tdd/native-langgraph-runtime/test_async_runtime.py`：Runtime/service/Gateway 异步兼容。
- `tests/tdd/native-langgraph-runtime/test_langsmith_native.py`：原生 tracing 装配与安全边界。
- `tests/tdd/native-langgraph-runtime/test_langsmith_experiment.py`：原生 graph Experiment。

**修改：**

- `runtime/assistant_loop_graph.py`、`graph_runtime.py`：`context_schema` 和 `Runtime.context`。
- `runtime/runtime.py`、`assistant_run_service.py`、`event_stream.py`：共享 prepare/finalize 与 async 主路径。
- `runtime/assistant_loop_nodes.py`、`tool_executor.py`：真实 LLM/Tool 调用的原生 child run。
- `observability/langsmith_config.py`、`trace_persistence.py`：native config，停止 LangSmith dual-tree composition。
- `evaluation/langsmith_trace.py`、`experiment_runtime.py`、`evals/langsmith_runtime_regression/*`：直接 graph target。
- 既有 core invariant 文件、三个 authority 文档和相关旧 TDD feature。

---

### Task 1: 稳定 Graph 身份、runtime context 与单次编译

**Files:**
- Create: `src/assistant_agent/runtime/assistant_graph_app.py`
- Create: `tests/tdd/native-langgraph-runtime/test_graph_app.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_graph.py`
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Modify: `src/assistant_agent/runtime/runtime.py`

**Interfaces:**
- Produces: `GraphExecutionIdentity.for_assistant_turn(*, agent_id: str, user_id: str, session_id: str, run_id: str) -> GraphExecutionIdentity`、`runnable_config() -> dict[str, dict[str, str]]`。
- Produces: `AssistantTurnGraphApp(checkpointer)` 及只读 `graph`。
- Produces: `GraphRuntimeContext` 作为 LangGraph `context_schema`。

- [ ] **Step 1: 写失败测试**

```python
def test_identity_has_stable_thread_and_run_namespace():
    one = GraphExecutionIdentity.for_assistant_turn(
        agent_id="a", user_id="u", session_id="s", run_id="r1"
    )
    two = GraphExecutionIdentity.for_assistant_turn(
        agent_id="a", user_id="u", session_id="s", run_id="r2"
    )
    assert one.thread_id == two.thread_id
    assert one.checkpoint_ns == "turn:r1"
    assert two.checkpoint_ns == "turn:r2"


def test_runtime_compiles_graph_once(monkeypatch):
    compiled = []
    monkeypatch.setattr(
        assistant_graph_app,
        "build_assistant_loop_graph",
        lambda **kw: compiled.append(kw) or _CompiledGraphProbe(),
    )
    runtime = _runtime()
    runtime.run_state(_request("one"))
    runtime.run_state(_request("two"))
    assert len(compiled) == 1
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_graph_app.py
```

Expected: FAIL，模块/身份不存在或仍逐请求编译。

- [ ] **Step 3: 实现身份与 graph app**

```python
@dataclass(frozen=True)
class GraphExecutionIdentity:
    thread_id: str
    checkpoint_ns: str
    run_id: str

    @classmethod
    def for_assistant_turn(cls, *, agent_id, user_id, session_id, run_id):
        raw = json.dumps(
            ["assistant", agent_id, user_id, session_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()[:32]
        return cls(f"assistant:{digest}", f"turn:{run_id}", run_id)

    def runnable_config(self):
        return {"configurable": {
            "thread_id": self.thread_id,
            "checkpoint_ns": self.checkpoint_ns,
            "run_id": self.run_id,
        }}


class AssistantTurnGraphApp:
    def __init__(self, *, checkpointer):
        self.graph = build_assistant_loop_graph(checkpointer=checkpointer)
```

- [ ] **Step 4: 改为 LangGraph context schema**

```python
graph = StateGraph(AssistantLoopState, context_schema=GraphRuntimeContext)
graph.add_node("assistant", bind_runtime_node("assistant", assistant_node))
graph.add_node("execute_tool", bind_runtime_node("execute_tool", execute_requested_tool_node))
graph.add_node("compose_response", bind_runtime_node("compose_response", compose_response_node))
return graph.compile(checkpointer=checkpointer, name="AssistantTurnGraph")
```

`bind_runtime_node(node_name, node_func)` 接收 `Runtime[GraphRuntimeContext]`，执行前临时注入依赖，返回前调用
`strip_runtime_context()`；移除按请求传 `runtime_context` 的闭包分支。`AgentGraphRuntime.__init__` 创建一个
`self.assistant_graph_app`。

- [ ] **Step 5: 验证 GREEN**

运行 Step 2 命令，再运行：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/core/integration/test_runtime_lifecycle.py
```

Expected: PASS，返回 graph state 不含 `RUNTIME_STATE_KEYS`。

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/runtime/{assistant_graph_app.py,assistant_loop_graph.py,graph_runtime.py,runtime.py} \
  tests/tdd/native-langgraph-runtime/test_graph_app.py
git commit -m "refactor(runtime): compile assistant graph once"
```

---

### Task 2: LangGraph v2 原生 stream consumer

**Files:**
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Modify: `tests/tdd/native-langgraph-runtime/test_graph_app.py`

**Interfaces:**
- Produces: `GraphStreamPart(type, namespace, data)`、`GraphStreamResult(final_state, parts)`。
- Produces: `AssistantTurnGraphApp.astream(input_state: AssistantLoopState, *, identity: GraphExecutionIdentity, context: GraphRuntimeContext) -> AsyncIterator[GraphStreamPart]` 和 `arun(input_state: AssistantLoopState, *, identity: GraphExecutionIdentity, context: GraphRuntimeContext) -> GraphStreamResult`。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_astream_uses_v2_and_preserves_namespace():
    probe = _AstreamProbe([
        {"type": "updates", "ns": ("worker:1",), "data": {"tool": {}}},
        {"type": "values", "ns": (), "data": {"state": "final"}},
    ])
    app = AssistantTurnGraphApp.from_compiled_graph(probe)
    parts = [p async for p in app.astream(
        {"state": "initial"}, identity=_identity(), context=_context()
    )]
    assert parts[0].namespace == ("worker:1",)
    assert probe.kwargs["version"] == "v2"
    assert probe.kwargs["subgraphs"] is True
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_graph_app.py -k astream
```

- [ ] **Step 3: 实现 stream**

```python
async for raw in self.graph.astream(
    input_state,
    config=identity.runnable_config(),
    context=context,
    stream_mode=["values", "updates", "messages", "custom", "tasks", "checkpoints"],
    subgraphs=True,
    version="v2",
):
    yield GraphStreamPart(
        type=str(raw["type"]),
        namespace=tuple(raw.get("ns") or ()),
        data=raw.get("data"),
    )
```

`arun()` 只接受 root namespace 的最后一个 `values` 作为终态；缺失时抛
`GraphExecutionError(code="graph_final_state_missing")`，不得返回部分状态。

- [ ] **Step 4: 增加缺失终态测试并运行整个文件**

Expected: 正常流 PASS；无 root final values 明确 fail-closed。

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/runtime/assistant_graph_app.py \
  tests/tdd/native-langgraph-runtime/test_graph_app.py
git commit -m "feat(runtime): add native LangGraph stream consumer"
```

---

### Task 3: 共享 prepare/finalize 并新增 `arun_state`

**Files:**
- Create: `tests/tdd/native-langgraph-runtime/test_async_runtime.py`
- Modify: `src/assistant_agent/runtime/runtime.py`

**Interfaces:**
- Produces: `AgentGraphRuntime.arun_state(request: UserRequest, event_sink: EventSink | None = None, cancel_token: Any | None = None, run_id: str | None = None) -> AgentState`。
- 保留: 相同运行参数的 `run_state() -> AgentState` 同步兼容。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_arun_state_uses_astream_not_invoke():
    runtime, probe = _runtime_with_graph_probe()
    state = await runtime.arun_state(_request("hello"), run_id="native")
    assert probe.astream_calls == 1
    assert probe.invoke_calls == 0
    assert state.status == "completed"


def test_sync_run_state_stays_compatible():
    runtime, probe = _runtime_with_graph_probe()
    assert runtime.run_state(_request("hello")).status == "completed"
    assert probe.invoke_calls == 1
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_async_runtime.py
```

- [ ] **Step 3: 提取共享运行结构**

```python
@dataclass
class _PreparedGraphRun:
    request: UserRequest
    state: AgentState
    initial_state: AssistantLoopState
    runtime_context: GraphRuntimeContext
    identity: GraphExecutionIdentity
    event_sink: EventSink | None
    started_at: float
    pre_terminal_state_hook: Callable[[AgentState], None] | None
```

实现 `_prepare_graph_run()`、`_execute_graph_sync()`、`_execute_graph_async()`、`_finalize_graph_run()`。
同步与异步共享 prepare/finalize；Deep Research submission 和 legacy durable quantum 保持当前特殊分支。

- [ ] **Step 4: 保持取消与 memory 生命周期**

两条入口都在 `finally` 中调用 `release_run_context(identity, run_id)`，并保留 pre/node/post graph 取消检查。

- [ ] **Step 5: 验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_async_runtime.py \
  tests/core/integration/test_runtime_lifecycle.py
```

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/runtime/runtime.py tests/tdd/native-langgraph-runtime/test_async_runtime.py
git commit -m "refactor(runtime): add native async graph execution"
```

---

### Task 4: Service/Gateway 直接消费异步 Runtime

**Files:**
- Modify: `src/assistant_agent/runtime/assistant_run_service.py`
- Modify: `src/assistant_agent/runtime/event_stream.py`
- Modify: `tests/tdd/native-langgraph-runtime/test_async_runtime.py`
- Modify: `tests/core/contract/test_gateway_contract.py`

**Interfaces:**
- Produces: `run_assistant_request_async(request: UserRequest, *, runtime: AgentGraphRuntime | None = None, event_sink: EventSink | None = None, cancel_token: Any | None = None, run_id: str | None = None) -> AssistantRunArtifacts`，其余 conversation 参数与同步入口同名同义。
- 保留: `run_assistant_request_stream(request: UserRequest, **同名关键字参数) -> AgentRunStream[AssistantRunArtifacts]` 和产品 `AgentEvent`。

- [ ] **Step 1: 写禁止主路径 `to_thread` 的失败测试**

```python
@pytest.mark.asyncio
async def test_service_stream_does_not_use_to_thread(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("native graph stream must not use to_thread")
    monkeypatch.setattr(asyncio, "to_thread", forbidden)
    stream = run_assistant_request_stream(_request("hello"), runtime=_AsyncRuntime())
    events = [event async for event in stream]
    artifacts = await stream.result()
    assert artifacts.state.status == "completed"
    assert events[-1].type == "final_response"
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_async_runtime.py -k to_thread
```

- [ ] **Step 3: 实现 async service orchestration**

新增 `run_assistant_request_async()`；把 request/conversation/realtime preparation 和 artifact/conversation
finalization 提成同步、异步共用 helper。`run_assistant_request_stream()` 的 task 直接 await 新函数。

- [ ] **Step 4: 简化同 loop 事件入队**

`AgentRunStream.emit()` 在当前 loop 直接 `put_nowait`，只有跨线程兼容调用使用 `call_soon_threadsafe`。

- [ ] **Step 5: 更新 `GATE-001`**

用 async Runtime probe 断言 ordered product events、final/cancel 兼容，且不泄漏 `GraphStreamPart`、checkpoint
或 task payload。不断言完整文案。

- [ ] **Step 6: 验证并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_async_runtime.py \
  tests/core/contract/test_gateway_contract.py
git add src/assistant_agent/runtime/{assistant_run_service.py,event_stream.py} \
  tests/tdd/native-langgraph-runtime/test_async_runtime.py tests/core/contract/test_gateway_contract.py
git commit -m "refactor(gateway): stream native async graph runs"
```

---

### Task 5: LangSmith 原生 graph/LLM/tool tracing

**Files:**
- Create: `src/assistant_agent/observability/langsmith_native.py`
- Create: `tests/tdd/native-langgraph-runtime/test_langsmith_native.py`
- Modify: `src/assistant_agent/observability/langsmith_config.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/tool_executor.py`

**Interfaces:**
- Produces: `native_langsmith_tracing(config, metadata, tags)`。
- Produces: `trace_llm_call(call: Callable[[], ChatResult], *, request: ChatRequest, provider: str, model: str) -> ChatResult`。
- Produces: `trace_governed_tool_call(call: Callable[[], ToolResult], *, tool_name: str, safe_input: dict[str, Any]) -> ToolResult`。

- [ ] **Step 1: 写失败测试**

```python
def test_active_experiment_tree_is_inherited(monkeypatch):
    calls = []
    monkeypatch.setattr(langsmith_native, "get_current_run_tree", lambda: object())
    monkeypatch.setattr(langsmith_native, "tracing_context", lambda **kw: calls.append(kw))
    with native_langsmith_tracing(_enabled_config(), metadata={"run_id": "r"}):
        pass
    assert calls == []


def test_llm_projection_excludes_callbacks_raw_payload_and_media_bytes():
    projected = project_llm_inputs(_unsafe_chat_request())
    assert "provider_request_callback" not in repr(projected)
    assert "base64" not in repr(projected)
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_langsmith_native.py
```

- [ ] **Step 3: 实现 native context**

若已有 RunTree，直接继承；若无父级且显式 enabled，以 `tracing_context(project_name, metadata, tags,
enabled=True, client=client)` 包裹 graph 调用并 bounded flush/close；默认日常 trace fail-open，Experiment
上下文 fail-closed。不修改进程全局环境变量。

- [ ] **Step 4: 包装真实调用边界**

`_run_chat_turn()` 只把 Provider invocation 包为 `traceable(name="llm.chat", run_type="llm")`；输入输出
processor 只允许 Provider-neutral messages/tools/model/usage，排除 credential、raw SDK envelope、hidden
reasoning、callback 和媒体字节。

`ToolExecutor.run_tool()` 只把 `execution_backend.execute` 的实际调用包为 tool child run；validation、authorization、
state lifecycle 和 commit 仍在治理链原位置。tool 输入使用现有 policy-safe summary。

- [ ] **Step 5: Graph app 进入 native context**

metadata 只含 `run_id`、hashed `thread_id`、`agent_id`、`execution_engine=assistant_turn_graph`；不把 raw
user/session ID 作为 tag。

- [ ] **Step 6: 验证并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_langsmith_native.py \
  tests/core/contract/test_observability_contract.py
git add src/assistant_agent/observability/{langsmith_native.py,langsmith_config.py} \
  src/assistant_agent/runtime/{assistant_graph_app.py,assistant_loop_nodes.py,tool_executor.py} \
  tests/tdd/native-langgraph-runtime/test_langsmith_native.py
git commit -m "feat(observability): trace native LangGraph runs in LangSmith"
```

---

### Task 6: Runtime Regression 直接评估原生 graph

**Files:**
- Create: `tests/tdd/native-langgraph-runtime/test_langsmith_experiment.py`
- Modify: `evals/langsmith_runtime_regression/experiment.py`
- Modify: `evals/langsmith_runtime_regression/cli.py`
- Modify: `src/assistant_agent/evaluation/experiment_runtime.py`
- Modify or Delete: `src/assistant_agent/evaluation/langsmith_trace.py`
- Modify: `tests/tdd/langsmith-parallel-evaluation/test_langsmith_runtime_regression.py`
- Modify: `tests/tdd/langsmith-parallel-evaluation/test_langsmith_runtime_regression_cli.py`
- Modify: `tests/tdd/langsmith-parallel-evaluation/test_langsmith_experiment_runtime.py`

**Interfaces:**
- Changes: `runtime_factory: Callable[[], RuntimeRegressionRuntime]`，不再接收 OTel binding。
- Produces: async Dataset target 和 `audit_native_graph_tree(runs: Sequence[Any], *, example_ids: tuple[str, ...]) -> NativeGraphCompletenessResult`。

- [ ] **Step 1: 写无 OTel binding 的失败测试**

```python
@pytest.mark.asyncio
async def test_dataset_target_runs_native_graph():
    runtime = _AsyncRuntime()
    result = await run_langsmith_runtime_regression_experiment(
        _AsyncEvaluateClient(),
        LangSmithRuntimeRegressionSettings(
            model="model", runtime_factory=lambda: runtime,
            run_name="native", git_commit="abc123",
        ),
    )
    assert runtime.requests[0].text == "重跑问题"
    assert result.example_ids == (str(EXAMPLE_ID),)
```

- [ ] **Step 2: 写 tree 完整性测试**

有效树必须是：

```text
experiment-item-task → AssistantTurnGraph → assistant → llm.chat
                                      └────→ compose_response
```

Tool 案例允许 `execute_tool → governed tool`。分别测试缺 graph、graph sibling、LLM 不在 graph subtree 时
fail-closed。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_langsmith_experiment.py
```

- [ ] **Step 4: 改用 `Client.aevaluate`**

async target 从当前 RunTree 读取 `reference_example_id`，创建 runtime，await `arun_state()`，返回现有
`langsmith_evaluator_output`，并在 `finally` 严格 close。CLI 顶层使用 `asyncio.run(_execute_async(client, args))`。

- [ ] **Step 5: 删除手工 OTel Experiment 装配**

LangSmith 路径不再调用 `create_langsmith_experiment_trace_store`、
`create_langsmith_text_otel_trace_observer_from_env`、`RuntimeTraceContext` 或
`LangSmithExperimentBinding.trace_context`。若 `langsmith_trace.py` 无调用点则 `git rm`。

- [ ] **Step 6: 更新旧临时测试**

按新原生契约更新 `tests/tdd/langsmith-parallel-evaluation/`，但不自动删除整个临时目录。

- [ ] **Step 7: 验证并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_langsmith_experiment.py \
  tests/tdd/langsmith-parallel-evaluation
git add evals/langsmith_runtime_regression src/assistant_agent/evaluation \
  tests/tdd/native-langgraph-runtime/test_langsmith_experiment.py \
  tests/tdd/langsmith-parallel-evaluation
git commit -m "refactor(eval): run LangSmith experiments on native graph"
```

---

### Task 7: 停止向 LangSmith 重建 canonical OTel tree

**Files:**
- Modify: `src/assistant_agent/observability/trace_persistence.py`
- Modify: `src/assistant_agent/observability/otel_exporter.py`
- Modify: `tests/tdd/langsmith-parallel-evaluation/test_dual_trace_export.py`
- Modify: `tests/core/contract/test_observability_contract.py`

**Interfaces:**
- Canonical trace 继续服务 local audit、查询和 Langfuse 兼容，但不再生成 LangSmith graph tree。

- [ ] **Step 1: 写失败测试**

```python
def test_server_store_does_not_register_langsmith_otel(monkeypatch):
    monkeypatch.setattr(
        persistence,
        "create_langsmith_text_otel_trace_observer_from_env",
        lambda: pytest.fail("native tracing owns LangSmith"),
    )
    persistence.create_server_trace_store(path=tmp_path / "trace.jsonl")
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_dual_trace_export.py
```

- [ ] **Step 3: 删除 dual-tree composition**

`create_server_trace_store()` 不再注册 LangSmith observer。保留 canonical store、local JSONL、Langfuse/通用
OTel 和业务 audit。Task 6 已无调用后删除 LangSmith Experiment store；仍有诊断调用则保留 deprecated
factory，但 composition root 不得调用，M5 删除。

- [ ] **Step 4: 更新 `OBS-001`**

core 断言 canonical run/session/tool correlation 仍有效，且 server store 不双写 LangSmith OTel；不在 core
模拟第三方 UI tree。

- [ ] **Step 5: 验证并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/langsmith-parallel-evaluation/test_dual_trace_export.py \
  tests/core/contract/test_observability_contract.py
git add src/assistant_agent/observability/{trace_persistence.py,otel_exporter.py} \
  tests/tdd/langsmith-parallel-evaluation/test_dual_trace_export.py \
  tests/core/contract/test_observability_contract.py
git commit -m "refactor(observability): stop rebuilding LangSmith graph traces"
```

---

### Task 8: 核心契约、authority 与 M1 验收

**Files:**
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/contract/test_gateway_contract.py`
- Modify: `tests/core/contract/test_observability_contract.py`
- Modify: `tests/core/INVARIANTS.md`（仅需澄清时）
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/observability-harness.md`
- Modify: `evals/README.md`
- Modify: `docs/authority.toml`（仅 route/verification 变化时）

**Interfaces:**
- Produces: M1 当前权威、core evidence、真实 LangSmith 验收入口和删除清单。

- [ ] **Step 1: 回补最小 core contract**

```text
RUN-001: sync/async invocation 产生相同合法终态；M1 尚无 suspended。
LOOP-001: AssistantTurnGraph 按 assistant/tool/compose 条件边推进。
IDENT-001: 同 session thread 稳定，不同 run namespace 隔离。
OBS-001: canonical audit 保持关联；LangSmith 不由 canonical OTel tree 重建。
GATE-001: 产品事件顺序/终态兼容且不泄漏 GraphStreamPart。
```

- [ ] **Step 2: 更新 authority**

`runtime-event-stream` 写明 `Agent-Service ← ProductEventProjector ← astream(v2)`；`observability-harness`
写明 LangSmith native graph trace 与 canonical audit 分工；`evals/README.md` 写明原生 tree 和 M1 尚未迁移
Langfuse Release Review。

- [ ] **Step 3: 运行临时 feature**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/langsmith-parallel-evaluation tests/tdd/langsmith-evaluator-automation
```

Expected: PASS。`native-langgraph-runtime` 是临时 RED/GREEN，用户可日后手动整目录删除。

- [ ] **Step 4: 运行共享核心安全网**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

Expected: PASS，正常机器小于 60 秒。

- [ ] **Step 5: 运行 authority validator**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

Expected: PASS。

- [ ] **Step 6: 真实 LangSmith 验收（仅获 operator 授权时）**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langsmith_runtime_regressions.py --inspect
MULTIMODAL_AGENT_PROVIDER_MODE=real /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langsmith_runtime_regressions.py --preflight \
  --allow-real-provider --allow-runtime-side-effects
MULTIMODAL_AGENT_PROVIDER_MODE=real /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langsmith_runtime_regressions.py --run \
  --run-name native-langgraph-m1-20260812-01 \
  --allow-real-provider --allow-runtime-side-effects
```

Expected: 每个 Example 恰有一个 task root，出现原生 `AssistantTurnGraph`、`assistant`、`llm.chat`、
`compose_response`，Tool 案例出现 governed tool descendant，并落库全部 required Feedback。未获授权时明确
报告未运行，不用 mock 冒充。

- [ ] **Step 7: 检查删除门槛**

```bash
rg -n "asyncio\.to_thread\(.*run_assistant_request|create_langsmith_experiment_trace_store|current_langsmith_experiment_binding" \
  src evals
```

Expected: service 主路径没有 `to_thread`；Experiment 没有 OTel store/binding 调用；deprecated 定义若保留，
不能有 composition-root 调用；每个 Runtime 实例只编译一个 graph。

- [ ] **Step 8: 提交文档与 core contract**

```bash
git add tests/core docs/runtime-event-stream-architecture.md docs/observability-harness.md \
  evals/README.md docs/authority.toml
git commit -m "docs: establish native LangGraph M1 contracts"
```

只 add 实际变化文件，不为 `review_required` 机械制造 diff。

---

## M1 完成报告格式

```text
完成内容：稳定 compiled graph、Runtime.context、astream(v2)、原生 LangSmith tree/Experiment。
Core invariant：RUN-001 / LOOP-001 / IDENT-001 / OBS-001 更新；GATE-001 行为保持。
Tests：列出实际命令、item count；临时 TDD 目录可由用户手动删除。
真实验证：说明是否获授权、Experiment 名称、tree 与 Feedback；未运行则明确报告。
删除结果：列出 to_thread 主路径、LangSmith OTel Experiment binding、dual-tree composition 的退出结果。
限制：M1 不承诺持久 checkpoint、interrupt/resume 或 Workflow DAG；M2/M3 继续。
```
