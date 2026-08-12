# 原生 LangGraph M5 全面收敛 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把普通 turn 的 state history、Replay、Fork、typed v2 streaming 与必要 interrupt 产品化，在 persistent/cutover/operator gate 全部满足后完成 Workflow 原生切换、LangSmith 等价验收和 Langfuse/影子 Runtime 退出。

**Architecture:** `AssistantTurnGraphApp` 与 `DurableWorkflowGraphApp` 继续是 compiled graph 的直接应用边界；历史选择使用安全的 opaque `history_ref`，Replay 直接运行 next 为 `prepare_invocation` 的历史 checkpoint config，Fork 只调用公开 `aupdate_state(..., as_node="time_travel_anchor")` 并验证返回 config 的 next 是同一 re-entry gate。短期执行事实归 checkpointer，长期记忆只归 `MemoryPluginHost`，业务副作用由独立 operation/publish barrier 守住；`AgentGraphRuntime` 最终只保留 composition、invoke/resume/time-travel facade，不再拥有 graph 已能承担的状态机职责。

**Tech Stack:** Python 3.11+、Pydantic v2、LangGraph 1.2.4 Graph API、LangSmith、FastAPI、pytest（mock/local/offline）、官方 async SQLite checkpointer（仅 Gate P1 获授权后引入）。

## Global Constraints

- 默认运行与 pytest 固定 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`；真实 Provider、LangSmith Dataset/Experiment 或外部副作用只能由 operator 显式开启。
- Agent-Service、Gateway、媒体 wire、安全授权、artifact ownership、业务审计与 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool` 必须保持。
- 产品 DTO、HTTP/WebSocket frame 和 `AgentEvent` 不得包含 `checkpoint_id`、`checkpoint_ns`、native task/interrupt ID、subgraph namespace、`StateSnapshot.config` 或完整 graph state。
- Graph checkpoint 只保存短期执行 state；长期记忆继续且只能经过 `MemoryPluginHost` 四生命周期。
- Replay 必须把 next 为 `prepare_invocation` 的历史 `StateSnapshot.config` 直接交给 compiled graph；Fork 必须使用公开 `aupdate_state(..., as_node="time_travel_anchor")` 并验证 native next 为 `prepare_invocation`，禁止访问 saver 私有结构、`checkpoint["channel_values"]` 或调用 `StateSnapshot.__copy__()`。
- Replay/Fork 每次使用新 `run_id`，保持 owner-bound `thread_id`/trace 语义；历史 checkpoint 中旧 `run_id` 只能作为已提交历史事实，任何可能从历史 checkpoint 直接恢复的 node 都必须先把它 re-enter 到 invocation-local `AgentState`，不能继续要求两者相等。
- `read` Tool 可按 graph policy 重放；`write|dangerous` Tool 和 publish/delivery 必须先通过 checkpointed stable operation scope 与持久 barrier，`outcome_unknown` 必须 fail closed。
- 不以 InMemory 测试声称跨进程 durability；persistent saver、production host/cutover、真实 LangSmith 等价、Langfuse 删除分别受 Gate P1–P4 约束。
- 不新增双平台 abstraction，不以 canonical OTel 或 stream event 重建 LangSmith graph tree。
- 临时 RED/GREEN 只放 `tests/tdd/native-langgraph-m5/`，用户可手动整目录删除；只有明确改变登记 invariant 才修改已有 core 测试。

---

## Gate 0：先冻结事实、执行顺序与不可越过条件

### 当前能力矩阵（2026-08-13 源码核实）

| Graph API 能力 | 当前状态 | M5 处理 |
| --- | --- | --- |
| StateGraph/State/Node/Edge/START/END/Conditional Edge | Implemented | 最终矩阵复验 |
| Command/Send/Reducer/Subgraph/Pregel/Super-step | Implemented | 最终矩阵复验 |
| Compile/Invoke/Runtime Context/Retry/Timeout/Fallback | Implemented | 最终矩阵复验 |
| Stream modes/subgraphs/durability | Partial | Task 5 类型化并统一 Assistant/Workflow |
| Checkpoint/Checkpointer/Thread/Interrupt/Resume | Partial（InMemory） | Task 1–6 离线；Task 8 persistent gate |
| Memory | Partial（Host 已有，Workflow 不消费） | Task 6 明确无需消费 graph store |
| Store | Partial（compile 参数为空能力） | Task 6 无真实 consumer 则删除 `store=` |
| Time Travel | Partial（只有 raw history） | Task 1–7 产品化 |
| Replay/Fork | Missing | Task 3/4 原生实现 |
| LangSmith | Partial（offline/prework） | Task 10 operator 等价 gate |
| production host/cutover | Missing | Task 9，依赖 Task 8 |

### 可立即执行（InMemory、mock/local/offline）

Task 1–7。它们只依赖仓库现有 LangGraph 1.2.4 与 `InMemorySaver`，能分别 RED/GREEN 和提交；不得等待 persistent saver 或真实 LangSmith。

### 阻塞 gate

| Gate | 必须先有的机器证据 | 解锁任务 | 不满足时行为 |
| --- | --- | --- | --- |
| P1 persistent dependency | operator 明确允许安装 `langgraph-checkpoint-sqlite`/其 async 依赖；锁定兼容版本；本机 async saver smoke 通过 | Task 8 | 保持 `none|memory`，不写自研 saver、不静默 memory fallback |
| P2 production cutover | P1 通过；同一 SQLite saver 跨两个 host 重建恢复通过；Graph/legacy 数据迁移与 rollback 窗口获批准 | Task 9 | production Deep Research 继续 legacy，禁止删除 scheduler |
| P3 LangSmith equivalence | 普通 turn Runtime Regression 与 Release Review、Durable Workflow Experiment 的真实 Dataset/Experiment/tree/Feedback 完整性全部通过，报告保存 experiment/project/run ID | Task 11 | Langfuse 仅冻结兼容，不删除任何仍被真实 operator 使用的链路 |
| P4 operator retirement | operator 确认 Langfuse webhook、runner、local stack、audit source 无真实消费者，并批准配置/依赖/部署删除 | Task 11/12 | 只输出 deletion inventory，不做破坏性退出 |

Gate 不是“稍后补测试”的占位符。执行者在对应 Task 开始时必须记录 `PASS` 或结构化 `BLOCKED`，并在同一记录的 `reason` 字段写具体缺失证据；`BLOCKED` 时停止该 Task，不提交半实现，也不能把总 M5 标记完成。

## 文件结构与职责锁定

- `src/assistant_agent/runtime/graph_time_travel.py`：安全 history DTO、opaque selector、Replay/Fork request 与 side-effect policy；不拥有 compiled graph。
- `src/assistant_agent/runtime/assistant_graph_app.py`：唯一调用 `aget_state_history`、历史 config、`aupdate_state`、`astream(..., version="v2")` 的普通 turn 原生应用边界。
- `src/assistant_agent/runtime/assistant_graph_state.py`：checkpoint state 与 invocation re-entry 投影；不保存 service/client/store。
- `src/assistant_agent/runtime/assistant_loop_graph.py`：稳定公开节点 `time_travel_anchor -> prepare_invocation` 与 graph topology；所有可选择历史边界的 native `next` 都先进入 re-entry gate。
- `src/assistant_agent/runtime/assistant_runtime_app.py`：真实普通 turn 的薄产品 facade；返回安全 DTO，不返回 native config/ID。
- `src/assistant_agent/workflows/durable_graph_app.py`：Workflow typed stream/history 与最终 snapshot；后续由 `WorkflowGraphHost` 持有。
- `src/assistant_agent/runtime/checkpointer.py`：唯一 saver factory；P1 后拥有 async persistent saver lifecycle。
- `src/assistant_agent/workflows/graph_host.py`：P2 后 production `DurableWorkflowGraphApp` composition/cutover owner。
- `evals/langsmith_runtime_regression/`、`evals/langsmith_workflow_regression/`、`evals/release_review/`：真实 graph target 与 LangSmith evaluator；不装配第二套 Runtime。
- `tests/tdd/native-langgraph-m5/`：M5 可删除的离线 RED/GREEN；不自动晋升 core。

---

### Task 1: 安全的 GraphCheckpointSummary 与 history selector

**Files:**
- Create: `src/assistant_agent/runtime/graph_time_travel.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Create: `tests/tdd/native-langgraph-m5/test_checkpoint_history.py`

**Interfaces:**
- Produces: `GraphCheckpointSelector(history_ref: str)`；`GraphCheckpointSummary(history_ref, created_at, status, next_nodes, has_interrupt, graph_version, state_schema_version)`。
- Produces: `AssistantTurnGraphApp.alist_history(identity, *, limit, before: GraphCheckpointSelector | None = None) -> tuple[GraphCheckpointSummary, ...]`。
- Internal only: `_resolve_history_snapshot(identity, selector) -> StateSnapshot`；原始 `snapshot.config` 不离开 app。
- Consumes: LangGraph `aget_state_history(config, before=historical_snapshot.config, limit=limit)`。
- Selection rule: 只为 `snapshot.next == ("prepare_invocation",)` 的 checkpoint 生成 selector；terminal、pre-input、直接指向 Provider/Tool/compose 的中间 checkpoint 不可被产品选择。

- [ ] **Step 1: 写安全投影 RED 测试**

```python
async def test_history_returns_opaque_selector_without_native_ids(runtime_probe):
    await runtime_probe.run("run-history-1")
    items = await runtime_probe.app.alist_history(runtime_probe.identity("inspect"), limit=10)
    assert items
    payload = items[0].model_dump(mode="json")
    assert payload["history_ref"].startswith("ghr_")
    assert not ({"checkpoint_id", "checkpoint_ns", "config", "values", "tasks"} & payload.keys())
```

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_checkpoint_history.py`

Expected: FAIL，`assistant_agent.runtime.graph_time_travel` 不存在。

- [ ] **Step 3: 实现严格 DTO 与不可逆 selector**

```python
class GraphCheckpointSelector(_StrictModel):
    history_ref: str = Field(pattern=r"^ghr_[0-9a-f]{32}$")

class GraphCheckpointSummary(_StrictModel):
    history_ref: str
    created_at: datetime
    status: Literal["running", "waiting_user", "completed", "failed", "cancelled"]
    next_nodes: tuple[str, ...]
    has_interrupt: bool
    graph_version: str
    state_schema_version: int

def graph_history_ref(*, thread_id: str, snapshot_config: Mapping[str, Any]) -> str:
    canonical = json.dumps({"thread_id": thread_id, "config": snapshot_config}, sort_keys=True, separators=(",", ":"))
    return "ghr_" + hashlib.sha256(canonical.encode()).hexdigest()[:32]
```

`alist_history()` 必须遍历 native history、验证 `AssistantTurnState`、仅保留 `next == ("prepare_invocation",)` 的 re-entry-safe checkpoint；为了返回 `limit` 个安全项可以分页读取更多 native history，但仍受总扫描上限约束。`before` 先在当前 owner/thread 的安全 history 中解析，再把对应 native config 传回 `aget_state_history`。未知、过期、跨 thread selector 返回 `graph_checkpoint_selector_not_found`，不能回显 selector 对应 config。

- [ ] **Step 4: GREEN 并检查泄漏**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_checkpoint_history.py`

Expected: PASS；递归 JSON 检查不存在 native ID/config/state。

- [ ] **Step 5: Commit**

```bash
git add src/assistant_agent/runtime/graph_time_travel.py src/assistant_agent/runtime/assistant_graph_app.py tests/tdd/native-langgraph-m5/test_checkpoint_history.py
git commit -m "feat: add safe graph checkpoint history"
```

### Task 2: invocation identity re-entry，解除历史 state 对旧 run_id 的强绑

**Files:**
- Modify: `src/assistant_agent/runtime/assistant_graph_state.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_graph.py`
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Create: `src/assistant_agent/runtime/graph_invocation_claims.py`
- Create: `tests/tdd/native-langgraph-m5/test_invocation_reentry.py`

**Interfaces:**
- Produces: stable public node names `time_travel_anchor` 与 `prepare_invocation`；`time_travel_anchor` 的唯一 edge 是 `prepare_invocation`，后者在任何 Provider/Tool/compose continuation 前提交 invocation identity。
- Produces: `reenter_assistant_invocation(value, *, runtime_state, invocation_kind: Literal["invoke", "resume", "replay", "fork"]) -> AssistantTurnState`，只由真实 `prepare_invocation` node 调用。
- Produces: `continuation: Literal["assistant", "await_input", "execute_tool", "compose_response", "end"]` checkpoint channel；`prepare_invocation` 按它走 conditional edge。
- Produces: `GraphInvocationClaimStore.claim(*, owner_digest, thread_id, run_id, invocation_kind, invocation_token) -> Literal["claimed", "same_invocation"]`；`InMemoryGraphInvocationClaimStore` 用锁和 `(owner_digest, thread_id, run_id)` 唯一键原子 claim，并把不可变 `invocation_token` 存为 value：相同 token 重入返回 `same_invocation`，不同 token 对同一 key 返回冲突。
- Consumes: `GraphRuntimeContext.invocation_claim_store` 与本次 invocation token；checkpoint 的 `invocation_run_ids` 仅供历史诊断，不作为跨 branch 唯一事实源。
- Produces: checkpoint fields `invocation_run_id: str`、`invocation_run_ids: tuple[str, ...]`、`invocation_kind`；`turn_origin_id` 保持逻辑 turn 身份。
- Consumes: `GraphRuntimeContext.agent_state` 的新 `run_id` 与历史 state 的 owner/trace/request facts。

- [ ] **Step 1: 写新 run re-entry RED**

```python
def test_prepare_invocation_reenters_same_turn_with_new_run_id(prepared_state, runtime_state):
    runtime_state.run_id = "run-replay-new"
    updated = reenter_assistant_invocation(
        prepared_state,
        runtime_state=runtime_state,
        invocation_kind="replay",
    )
    assert updated["turn_origin_id"] == prepared_state["turn_origin_id"]
    assert updated["invocation_run_id"] == "run-replay-new"
    assert updated["run"]["trace_id"] == prepared_state["run"]["trace_id"]

async def test_one_tool_loop_crosses_gate_repeatedly_but_reenters_once(probe):
    result = await probe.run_tool_then_answer(run_id="run-loop")
    assert probe.stream_node_order == [
        "prepare_invocation", "assistant",
        "time_travel_anchor", "prepare_invocation", "execute_tool",
        "time_travel_anchor", "prepare_invocation", "assistant",
        "time_travel_anchor", "prepare_invocation", "compose_response",
        "time_travel_anchor", "prepare_invocation",
    ]
    assert result.final_state["invocation_run_ids"].count("run-loop") == 1

async def test_same_run_id_cannot_be_claimed_from_two_historical_branches(probe):
    selector = await probe.safe_history_ref()
    results = await asyncio.gather(
        probe.replay(selector, run_id="run-duplicate"),
        probe.fork(selector, run_id="run-duplicate"),
        return_exceptions=True,
    )
    assert sum(isinstance(item, GraphStreamResult) for item in results) == 1
    assert [item.code for item in results if isinstance(item, GraphExecutionError)] == [
        "graph_invocation_run_id_reused"
    ]

def test_claim_store_distinguishes_same_invocation_from_competing_branch(store):
    assert store.claim(**CLAIM, invocation_token="token-a") == "claimed"
    assert store.claim(**CLAIM, invocation_token="token-a") == "same_invocation"
    with pytest.raises(GraphInvocationClaimConflict):
        store.claim(**CLAIM, invocation_token="token-b")
```

测试必须从一次真实 stream 收集 `updates`，spy/assert `prepare_invocation` update 出现在任何 `assistant/execute_tool/compose_response` update 之前；同一次 invocation 在每个 semantic transition 再次经过 gate 时必须幂等 no-op。owner/request/profile/schema 不一致 fail closed；历史已消费 run ID 不能被另一次 Replay/Fork 重新认领。

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_invocation_reentry.py`

Expected: FAIL，state 尚无 invocation 字段且 graph 无 `prepare_invocation`。

- [ ] **Step 3: 升级 state schema 与加入稳定节点**

```python
def prepare_invocation_node(
    state: AssistantTurnState,
    runtime: Runtime[GraphRuntimeContext],
) -> AssistantTurnState:
    runtime_state = runtime.context.agent_state
    if runtime_state is None:
        raise AssistantStateCompatibilityError("Invocation-local AgentState is required.")
    return reenter_assistant_invocation(
        state,
        runtime_state=runtime_state,
        invocation_kind=runtime.context.invocation_kind,
    )
```

拓扑改为：

```text
START -> prepare_invocation -> continuation 指定的语义节点
assistant/await_input/execute_tool/compose_response
      -> time_travel_anchor -> prepare_invocation -> 下一 continuation
```

每个语义 node 只写下一步 `continuation`，不直接跳到另一个语义 node；`time_travel_anchor` 是无副作用稳定 public node，唯一 edge 指向 `prepare_invocation`。因此每个可产品选择的 checkpoint 的 native `next` 都是 `prepare_invocation`。gate 严格核对 owner/request/profile/trace，并先用 claim store 原子 claim `(owner_digest, thread_id, run_id)`：同一 invocation token 的后续循环返回 `same_invocation` 并幂等复核；另一个 Replay/Fork branch 即使从更早 checkpoint 出发，也不能复用已经 claim 的 run ID。首次 claim 后才保留历史 trajectory 与 `turn_origin_id`、把 checkpoint-safe `run.run_id` 切换为新 invocation，再按 continuation 路由；不得在 Provider/Tool/compose 调用之后才切换。升级 `graph_version/state_schema_version`，旧 schema 必须明确迁移或报 `graph_checkpoint_incompatible`。

- [ ] **Step 4: GREEN 与 topology mutation 检查**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_invocation_reentry.py tests/tdd/native-langgraph-m2/test_checkpoint_state.py`

Expected: PASS；普通 Tool loop 可多次经过同一 invocation gate 且只登记一次；临时让任一语义 node 直接跳过 anchor/gate 时 topology/stream-order 测试失败，恢复后通过。

- [ ] **Step 5: Commit**

```bash
git add src/assistant_agent/runtime/assistant_graph_state.py src/assistant_agent/runtime/assistant_loop_nodes.py src/assistant_agent/runtime/assistant_loop_graph.py src/assistant_agent/runtime/graph_runtime.py src/assistant_agent/runtime/graph_invocation_claims.py tests/tdd/native-langgraph-m5/test_invocation_reentry.py
git commit -m "feat: add graph invocation reentry"
```

### Task 3: 原生 Replay（历史 config，不复制 state）

**Files:**
- Modify: `src/assistant_agent/runtime/graph_time_travel.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Create: `tests/tdd/native-langgraph-m5/test_native_replay.py`

**Interfaces:**
- Produces: `GraphReplayRequest(selector: GraphCheckpointSelector)`。
- Produces: `AssistantTurnGraphApp.areplay(*, identity, context, request, part_consumer=None) -> GraphStreamResult`。
- Produces internal: `astream(..., runnable_config: Mapping[str, Any] | None = None)` 与 `_consume_stream(..., runnable_config: Mapping[str, Any] | None = None)`；`_consume_stream` 必须把参数原样转交 `astream`，后者只合并 callbacks/metadata/tags，绝不覆盖 `configurable.thread_id/checkpoint_id/checkpoint_ns`。
- Consumes: Task 1 `_resolve_history_snapshot()` 与 Task 2 invocation re-entry。
- Native contract: `_consume_stream(None, runnable_config=_invocation_config_from_snapshot(snapshot, identity, callbacks), ...)`。

- [ ] **Step 1: 写 Replay RED**

```python
async def test_replay_runs_from_historical_config_with_new_invocation_id(probe):
    first = await probe.run_to_completion("run-original")
    selected = (await probe.app.alist_history(probe.identity("inspect"), limit=20))[2]
    replayed = await probe.app.areplay(
        identity=probe.identity("run-replay"),
        context=probe.context("run-replay", invocation_kind="replay"),
        request=GraphReplayRequest(selector={"history_ref": selected.history_ref}),
    )
    assert replayed.final_state["invocation_run_id"] == "run-replay"
    assert replayed.final_state["turn_origin_id"] == first.final_state["turn_origin_id"]
```

spy 必须 monkeypatch compiled `graph.astream()`，断言 input 是 `None`、收到的 `configurable.thread_id/checkpoint_id/checkpoint_ns` 与历史 snapshot config 完全一致，且实现源码不含 `__copy__`、`channel_values` 或 saver storage 访问。

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_native_replay.py`

Expected: FAIL，`areplay` 不存在。

- [ ] **Step 3: 实现 historical-config Replay**

```python
snapshot = await self._resolve_history_snapshot(identity, request.selector)
if tuple(snapshot.next or ()) != ("prepare_invocation",):
    raise GraphExecutionError("graph_checkpoint_not_replayable", "The selected checkpoint has no safe re-entry gate.")
config = self._invocation_config_from_snapshot(snapshot, identity, callbacks)
return await self._consume_stream(
    None,
    identity=identity,
    context=context.with_invocation_kind("replay"),
    runnable_config=config,
    part_consumer=part_consumer,
)
```

`_invocation_config_from_snapshot()` 只在进程内复制普通 mapping 并覆盖 metadata/callbacks/tags；不得修改 `configurable.thread_id/checkpoint_id/checkpoint_ns`，也不得把 config 放进 result。选择 terminal checkpoint、未知 selector、owner/profile/schema 不匹配、复用 run ID均返回稳定错误。

- [ ] **Step 4: GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_native_replay.py tests/tdd/native-langgraph-m2/test_interrupt_resume.py`

Expected: PASS；Replay trajectory 从选中 historical config 的真实 `prepare_invocation` 开始，stream spy 证明 gate update 先于任何 Provider/Tool/compose update，且不从 current latest state 开始。

- [ ] **Step 5: Commit**

```bash
git add src/assistant_agent/runtime/graph_time_travel.py src/assistant_agent/runtime/assistant_graph_app.py tests/tdd/native-langgraph-m5/test_native_replay.py
git commit -m "feat: add native graph replay"
```

### Task 4: 原生 Fork（公开 aupdate_state + 稳定 as_node）

**Files:**
- Modify: `src/assistant_agent/runtime/graph_time_travel.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Create: `tests/tdd/native-langgraph-m5/test_native_fork.py`

**Interfaces:**
- Produces: `GraphForkPatch(request_text: str | None, response_style: str | None)`；只允许产品拥有、checkpoint-safe、无权限扩张的字段。
- Produces: `GraphForkRequest(selector, patch)`。
- Produces: `AssistantTurnGraphApp.afork(..., request: GraphForkRequest) -> GraphStreamResult`。
- Native contract: `new_config = await graph.aupdate_state(historical_config, values, as_node="time_travel_anchor")`；因为该稳定 public node 的唯一 edge 是 `prepare_invocation`，返回 config 必须以 gate 为 next，随后才运行。

- [ ] **Step 1: 写 Fork RED**

```python
async def test_fork_uses_public_update_state_and_stable_node(probe, monkeypatch):
    calls = []
    returned_configs = []
    original = probe.app.graph.aupdate_state
    async def recording(config, values, as_node=None, task_id=None):
        calls.append((config, values, as_node, task_id))
        returned = await original(config, values, as_node=as_node, task_id=task_id)
        returned_configs.append(returned)
        return returned
    monkeypatch.setattr(probe.app.graph, "aupdate_state", recording)
    result = await probe.fork(request_text="branch request", run_id="run-fork")
    assert calls[0][2] == "time_travel_anchor"
    assert tuple((await probe.app.graph.aget_state(returned_configs[0])).next) == ("prepare_invocation",)
    assert probe.stream_node_order[:2] == ["prepare_invocation", "assistant"]
    assert probe.astream_configs[0]["configurable"] == returned_configs[0]["configurable"]
    assert probe.consumed_parts == probe.emitted_parts
    assert result.final_state["invocation_run_id"] == "run-fork"
```

另测 patch 不能改变 user/session/agent/profile/catalog/capability refs/tool result/operation scope。

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_native_fork.py`

Expected: FAIL，`afork` 不存在。

- [ ] **Step 3: 实现公开 Fork**

```python
historical = await self._resolve_history_snapshot(identity, request.selector)
values = fork_patch_for_assistant_state(
    validate_assistant_turn_state(historical.values),
    request.patch,
)
fork_config = await self._graph.aupdate_state(
    historical.config,
    values,
    as_node="time_travel_anchor",
)
fork_snapshot = await self._graph.aget_state(fork_config)
if tuple(fork_snapshot.next or ()) != ("prepare_invocation",):
    raise GraphExecutionError("graph_fork_reentry_missing", "Fork did not enter the invocation gate.")
return await self._consume_stream(
    None,
    runnable_config=fork_config,
    identity=identity,
    context=context.with_invocation_kind("fork"),
)
```

Fork patch 只改 `request_text/response_style/continuation` 等允许产品字段，不预写 `run_id` 或 invocation claim；真实 `prepare_invocation` gate 是 run identity 的唯一写入点。Fork 产生 native branch config 但产品只得到最终结果和新的 opaque history refs；内部 checkpoint ID 不返回。`as_node="time_travel_anchor"` 固定在 app 内，不接受调用方覆盖；RED/GREEN 必须 monkeypatch compiled `graph.astream` 证明执行 config 的 configurable 与 `aupdate_state` 返回值完全相等、从 `updates` 证明 gate 先执行，并证明统一 `part_consumer` 收到全部 part，不能只凭最终 `run_id` 推断。

- [ ] **Step 4: GREEN 与私有 API 禁用检查**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_native_fork.py`

Run: `rg -n '__copy__|channel_values|\.storage|\.serde' src/assistant_agent/runtime/assistant_graph_app.py src/assistant_agent/runtime/graph_time_travel.py`

Expected: pytest PASS；`rg` 零命中。

- [ ] **Step 5: Commit**

```bash
git add src/assistant_agent/runtime/graph_time_travel.py src/assistant_agent/runtime/assistant_graph_app.py tests/tdd/native-langgraph-m5/test_native_fork.py
git commit -m "feat: add native graph fork"
```

### Task 5: typed v2 streaming modes、subgraphs 与 durability

**Files:**
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Modify: `src/assistant_agent/workflows/durable_graph_app.py`
- Modify: `src/assistant_agent/runtime/product_event_projector.py`
- Create: `tests/tdd/native-langgraph-m5/test_typed_v2_stream.py`

**Interfaces:**
- Produces: `GraphStreamMode = Literal["values", "updates", "messages", "custom", "tasks", "checkpoints"]`。
- Produces: discriminated `GraphValuesPart | GraphUpdatePart | GraphMessagePart | GraphCustomPart | GraphTaskPart | GraphCheckpointPart`，统一字段 `type/namespace/data`。
- Produces: `GraphStreamOptions(modes, include_subgraphs, durability: Literal["sync", "async", "exit"] | None)`；app 自己提供受信默认值，产品调用者不能任意打开 debug/state wire。
- Consumes: LangGraph `astream(..., version="v2", stream_mode=..., subgraphs=..., durability=...)`。

- [ ] **Step 1: 写 typed stream RED**

```python
async def test_assistant_and_workflow_emit_validated_v2_parts(probes):
    assistant_parts = await probes.assistant_parts()
    workflow_parts = await probes.workflow_parts()
    assert all(part.type in get_args(GraphStreamMode) for part in assistant_parts + workflow_parts)
    assert any(part.namespace for part in workflow_parts)
    assert not any("checkpoint_id" in json.dumps(project_product(part)) for part in assistant_parts)
```

另测未知 type、非法 namespace/data 形状 fail closed；`messages` 只在 Assistant 打开，Workflow 默认不伪造。

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_typed_v2_stream.py`

Expected: FAIL，现有 part 的 `type: str/data: Any` 未验证。

- [ ] **Step 3: 实现统一 parser 与 app 默认策略**

```python
def parse_graph_stream_part(raw: Mapping[str, Any]) -> GraphStreamPart:
    envelope = TypeAdapter(GraphStreamEnvelope).validate_python(
        {"type": raw.get("type"), "namespace": tuple(raw.get("ns") or ()), "data": raw.get("data")}
    )
    return envelope.root
```

Assistant 默认 modes 为 `values/updates/messages/custom/tasks/checkpoints`、`subgraphs=True`；Workflow 为 `values/updates/custom/tasks/checkpoints`、`subgraphs=True`、`durability="sync"`。Replay/Fork 复用同一 parser/options，不另造 stream。

- [ ] **Step 4: GREEN 与产品投影兼容**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_typed_v2_stream.py tests/tdd/native-langgraph-m2/test_product_event_projector.py tests/tdd/native-langgraph-m3/test_workflow_product_projection.py`

Expected: PASS；产品 projector 仍只接受 `RuntimeProductFact`/Workflow product fact，不直接消费 checkpoint/task/debug part。

- [ ] **Step 5: Commit**

```bash
git add src/assistant_agent/runtime/assistant_graph_app.py src/assistant_agent/workflows/durable_graph_app.py src/assistant_agent/runtime/product_event_projector.py tests/tdd/native-langgraph-m5/test_typed_v2_stream.py
git commit -m "refactor: type native graph v2 streams"
```

### Task 6: Memory/Store 职责与 time-travel side-effect barrier

**Files:**
- Modify: `src/assistant_agent/workflows/durable_graph.py`
- Modify: `src/assistant_agent/workflows/graph_context.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Modify: `src/assistant_agent/runtime/graph_time_travel.py`
- Modify: `src/assistant_agent/runtime/tool_operation_barrier.py`
- Modify: `src/assistant_agent/workflows/graph_publish.py`
- Create: `tests/tdd/native-langgraph-m5/test_time_travel_side_effects.py`
- Create: `tests/tdd/native-langgraph-m5/test_memory_store_boundaries.py`

**Interfaces:**
- Produces: `TimeTravelEffectPolicy.classify(state, next_nodes) -> Literal["safe", "barrier_required", "outcome_unknown", "forbidden"]`。
- Consumes: `ToolSpec.category`、checkpointed `operation_scope_id`、`SQLiteToolOperationStore` 与 `SQLiteWorkflowPublishStore`。
- Removes: `build_durable_workflow_graph(..., store: Any | None = None)` 及 `compile(store=store)`，因为当前没有 `runtime.store` consumer。
- Preserves: `MemoryPluginHost.open_session/prepare_context/ingest_turn/close_session`；不得把它接到 LangGraph Store。

- [ ] **Step 1: 写边界 RED**

```python
def test_workflow_graph_does_not_compile_an_unused_store():
    signature = inspect.signature(build_durable_workflow_graph)
    assert "store" not in signature.parameters

async def test_replay_stops_before_unknown_write_outcome(probe):
    selected = await probe.checkpoint_before_write()
    probe.operation_store.mark_outcome_unknown(selected.operation_key)
    with pytest.raises(GraphExecutionError) as exc:
        await probe.replay(selected.history_ref)
    assert exc.value.code == "graph_time_travel_effect_outcome_unknown"
    assert probe.write_tool.calls == 1

async def test_fork_stops_before_unknown_write_outcome(probe):
    selected = await probe.checkpoint_before_write()
    probe.operation_store.mark_outcome_unknown(selected.operation_key)
    with pytest.raises(GraphExecutionError) as exc:
        await probe.fork(selected.history_ref, request_text="forked input")
    assert exc.value.code == "graph_time_travel_effect_outcome_unknown"
    assert probe.write_tool.calls == 1
    assert probe.provider.calls == 0
```

另测长期 memory contribution 不写 checkpoint、Replay/Fork 不再次调用 `prepare_context`/`ingest_turn`，只由真实新 turn 触发生命周期。

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_time_travel_side_effects.py tests/tdd/native-langgraph-m5/test_memory_store_boundaries.py`

Expected: FAIL，unused `store` 参数仍存在且 time-travel policy 尚无统一入口。

- [ ] **Step 3: 实现 fail-closed barrier 与删除空 Store 装配**

```python
decision = effect_policy.classify(snapshot_state, tuple(snapshot.next or ()))
if decision == "outcome_unknown":
    raise GraphExecutionError(
        "graph_time_travel_effect_outcome_unknown",
        "The selected checkpoint cannot safely repeat an unresolved side effect.",
    )
if decision == "forbidden":
    raise GraphExecutionError("graph_time_travel_effect_forbidden", "The selected checkpoint is not replayable.")
```

`AssistantTurnGraphApp.areplay()` 在 resolve historical snapshot 后、调用 `_consume_stream` 前执行 guard；`afork()` 在验证 patch/selected snapshot 后、调用 `aupdate_state` 前执行同一 guard。两条路径的 `outcome_unknown/forbidden` 都必须在 Provider、Tool、compose、publish 调用计数增加前抛错。`read` 可重放；`write|dangerous` 必须保持原 `thread_id + operation_scope_id + profile + tool name + input digest`，barrier 的 succeeded/failed/outcome_unknown 语义不变。publish/delivery 使用其稳定 operation key；不得用新 run ID 生成新 operation。Graph Store 只有出现跨 graph node 的真实 namespace/key/value 长期数据需求并通过 Memory authority 评审时才重新引入。

- [ ] **Step 4: GREEN 与治理回归**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_time_travel_side_effects.py tests/tdd/native-langgraph-m5/test_memory_store_boundaries.py tests/tdd/native-langgraph-m2/test_tool_operation_barrier.py tests/tdd/native-langgraph-m3/test_workflow_publish_barrier.py`

Expected: PASS；副作用 probe 调用次数不增加。

- [ ] **Step 5: Commit**

```bash
git add src/assistant_agent/workflows/durable_graph.py src/assistant_agent/workflows/graph_context.py src/assistant_agent/runtime/assistant_graph_app.py src/assistant_agent/runtime/graph_time_travel.py src/assistant_agent/runtime/tool_operation_barrier.py src/assistant_agent/workflows/graph_publish.py tests/tdd/native-langgraph-m5/test_time_travel_side_effects.py tests/tdd/native-langgraph-m5/test_memory_store_boundaries.py
git commit -m "fix: guard graph time travel side effects"
```

### Task 7: 普通 turn 的 time-travel 产品 facade（不扩张 wire）

**Files:**
- Modify: `src/assistant_agent/runtime/assistant_runtime_app.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/assistant_run_service.py`
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Create: `tests/tdd/native-langgraph-m5/test_assistant_time_travel_facade.py`
- Modify: `docs/runtime-event-stream-architecture.md`

**Interfaces:**
- Produces: `AssistantRuntimeApp.list_turn_history(owner, *, limit, before=None) -> tuple[GraphCheckpointSummary, ...]`。
- Produces: `AssistantRuntimeApp.replay_turn(owner, request: GraphReplayRequest, *, run_id) -> AgentState`。
- Produces: `AssistantRuntimeApp.fork_turn(owner, request: GraphForkRequest, *, run_id) -> AgentState`。
- Consumes: 同一个 process-owned `AssistantTurnGraphApp`、session/capability/context composition 与 Task 1–6 policy。
- Produces internal: `_prepare_graph_continuation(request, *, run_id, invocation_kind: Literal["resume", "replay", "fork"], ...) -> _PreparedGraphRun`；只重建 invocation-local `AgentState`、Tool/context/artifact service refs 和已冻结的 checkpoint refs，不调用 Memory Plugin lifecycle。
- Explicit non-interface: 不新增 HTTP/WebSocket route，不把 selector/native IDs 塞进 `AgentEvent` 或 Gateway frame；有真实客户端需求时另立协议设计。

- [ ] **Step 1: 写真实 composition facade RED**

```python
async def test_runtime_app_products_use_shared_compiled_graph(app_probe):
    await app_probe.run_standard_turn(run_id="run-product")
    history = await app_probe.app.list_turn_history(app_probe.owner, limit=10)
    replayed = await app_probe.app.replay_turn(
        app_probe.owner,
        GraphReplayRequest(selector={"history_ref": history[1].history_ref}),
        run_id="run-product-replay",
    )
    assert replayed.run_id == "run-product-replay"
    assert app_probe.compiled_graph_count == 1
    assert app_probe.memory_call_delta("replay") == {"open_session": 0, "prepare_context": 0, "ingest_turn": 0}
    assert app_probe.memory_call_delta("fork") == {"open_session": 0, "prepare_context": 0, "ingest_turn": 0}
    assert app_probe.memory_call_delta("resume_until_terminal") == {"open_session": 0, "prepare_context": 0, "ingest_turn": 1}
```

另测未授权 owner、默认无 saver、interrupt 未启用、selector 跨 session 均稳定拒绝。

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_assistant_time_travel_facade.py`

Expected: FAIL，产品 facade 不存在。

- [ ] **Step 3: 复用现有 preparation/finalization，收窄 Runtime**

`AgentGraphRuntime` 增加内部 `alist_history/areplay_state/afork_state` 委托。新 turn 仍调用 `_prepare_graph_run()` 并拥有 `open_session/prepare_context/ingest_turn`；resume/replay/fork 固定调用 `_prepare_graph_continuation(..., invocation_kind=...)`，它从已验证 checkpoint refs 与当前 process-owned services 重建 `GraphRuntimeContext`，preparation 不调用三项 Memory lifecycle。finalization 按强类型 kind 分开：`resume` 延续尚未完成的原 turn，成功终态恰好 `ingest_turn` 一次；`replay/fork` 是派生执行，不 `ingest_turn`；三者都不再次 open/prepare。测试记录各 operation 前后计数 delta，不断言正常原 turn 的累计总数为零。两条 preparation 复用无副作用 helper，不以 bool 默认值模糊区分。`AssistantRuntimeApp` 做 owner/session 校验并持有 lifecycle。

```python
async def replay_turn(self, owner, request, *, run_id):
    runtime = self._runtime_for_owner(owner)
    prepared = runtime._prepare_graph_continuation(
        owner.to_user_request(), request=request, run_id=run_id, invocation_kind="replay"
    )
    return await runtime.areplay_state(prepared=prepared)
```

- [ ] **Step 4: GREEN、core 兼容与文档同步**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_assistant_time_travel_facade.py tests/core/integration/test_runtime_lifecycle.py tests/core/contract/test_gateway_contract.py`

Expected: PASS；`RUN-001/LOOP-001/IDENT-001/GATE-001` 行为不回退。更新 authority 明确 time travel 是内部产品 service，wire 仍无 resume/time-travel。

- [ ] **Step 5: Commit**

```bash
git add src/assistant_agent/runtime/assistant_runtime_app.py src/assistant_agent/runtime/runtime.py src/assistant_agent/runtime/assistant_run_service.py src/assistant_agent/runtime/graph_runtime.py tests/tdd/native-langgraph-m5/test_assistant_time_travel_facade.py docs/runtime-event-stream-architecture.md
git commit -m "feat: productize assistant graph time travel"
```

### Task 8: Gate P1——官方 async persistent saver 与跨 host 恢复

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/assistant_agent/config.py`
- Modify: `src/assistant_agent/runtime/checkpointer.py`
- Modify: `src/assistant_agent/runtime/runtime_host.py`
- Modify: `src/assistant_agent/runtime/graph_invocation_claims.py`
- Create: `tests/tdd/native-langgraph-m5/test_persistent_checkpointer.py`
- Modify: `.env.example`
- Modify: `docs/runtime-event-stream-architecture.md`

**Interfaces:**
- Produces: `AsyncCheckpointerOwner` async context/lifecycle，唯一创建与关闭官方 `AsyncSqliteSaver`。
- Produces: `SQLiteGraphInvocationClaimStore`，在独立业务 SQLite unique constraint `(owner_digest, thread_id, run_id)` 上原子 claim；它与 checkpointer 同 lifecycle owner 但不伪装成 checkpoint channel。
- Produces: configured backend `none|memory|sqlite`；`sqlite` 缺依赖、路径或 owner 时 fail closed。
- Consumes: operator 批准的 `langgraph-checkpoint-sqlite` 版本与本机未跟踪 SQLite path。

- [ ] **Step 1: 记录 Gate P1**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -c "import importlib.util; print(importlib.util.find_spec('langgraph.checkpoint.sqlite.aio'))"`

Expected: 只有 operator 已授权依赖且输出非 `None` 才继续；否则记录 `BLOCKED(P1: official async sqlite saver unavailable/not authorized)` 并停止本 Task。

- [ ] **Step 2: 写跨 host RED**

```python
async def test_sqlite_saver_recovers_same_thread_in_fresh_host(tmp_path):
    async with build_host(tmp_path / "checkpoints.sqlite3") as first:
        waiting = await first.interrupt("run-before")
    async with build_host(tmp_path / "checkpoints.sqlite3") as second:
        resumed = await second.resume(waiting.owner, "run-after")
    assert resumed.status == "completed"
    assert resumed.run_id == "run-after"

async def test_rebuilt_hosts_cannot_reuse_claimed_invocation_run_id(tmp_path):
    first = await build_host(tmp_path).open()
    await first.replay(run_id="run-claimed")
    await first.close()
    second = await build_host(tmp_path).open()
    with pytest.raises(GraphExecutionError, match="graph_invocation_run_id_reused"):
        await second.fork(run_id="run-claimed")
```

- [ ] **Step 3: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_persistent_checkpointer.py`

Expected: FAIL，factory 仅支持 MemorySaver。

- [ ] **Step 4: 实现官方 saver lifecycle 并 GREEN**

```python
@asynccontextmanager
async def open_checkpointer(config: ProviderConfig):
    if config.langgraph_checkpointer_backend == "sqlite":
        async with AsyncSqliteSaver.from_conn_string(str(config.langgraph_checkpoint_path)) as saver:
            await saver.setup()
            yield saver
        return
    yield create_nonpersistent_checkpointer(config)
```

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_persistent_checkpointer.py tests/tdd/native-langgraph-m2/test_runtime_resume.py`

Expected: PASS；重建 host 后 resume/history/replay/fork 仍成立，`memory` 不被误报 persistent。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/assistant_agent/config.py src/assistant_agent/runtime/checkpointer.py src/assistant_agent/runtime/runtime_host.py src/assistant_agent/runtime/graph_invocation_claims.py tests/tdd/native-langgraph-m5/test_persistent_checkpointer.py .env.example docs/runtime-event-stream-architecture.md
git commit -m "feat: add official persistent graph saver"
```

当前 checkout 没有依赖 lock file；如果执行期仓库已新增正式 lock file，先在 Task 8 的 Files 清单中写出其准确路径，再显式加入同一提交，不得静默忽略 lock 更新。

### Task 9: Gate P2——WorkflowGraphHost、production cutover 与 legacy scheduler 删除

**Files:**
- Create: `src/assistant_agent/workflows/graph_host.py`
- Create: `src/assistant_agent/workflows/cutover.py`
- Modify: `src/assistant_agent/api/app.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/api/routes_workflows.py`
- Modify: `src/assistant_agent/workflows/service.py`
- Modify: `src/assistant_agent/workflows/store.py`
- Modify: `src/assistant_agent/workflows/sqlite_store.py`
- Modify: `src/assistant_agent/workflows/models.py`
- Delete after cutover proof: `src/assistant_agent/workflows/worker.py`
- Delete after cutover proof: `src/assistant_agent/workflows/runtime.py`
- Delete after cutover proof: `src/assistant_agent/workflows/execution.py`
- Modify: `tests/tdd/native-langgraph-m3/test_no_deep_research_scheduler.py`
- Create: `tests/tdd/native-langgraph-m5/test_workflow_graph_host.py`
- Create: `tests/tdd/native-langgraph-m5/test_workflow_cutover_manifest.py`
- Modify: `docs/tool-calling-architecture.md`

**Interfaces:**
- Produces: `WorkflowGraphHost.start/resume/cancel/get_status/get_events/get_result/close`，内部持有 process-owned saver、compiled graph、artifact/publish/operation services。
- Produces: `WorkflowEngineCutoverManifest(schema_version="workflow_engine_cutover_v1", phase, new_submission_engine, legacy_rules, drain_deadline, rollback_deadline)` 与 `WorkflowCutoverController`；manifest 来自 operator 本机未跟踪配置并以 digest 写业务审计，不写死部署时间。
- Manifest phase 固定 `cutover_active | rollback_requested | draining | retired`；只有 operator CAS 切到 `rollback_requested` 才允许 prepared/no-checkpoint rollback，`cutover_active` 永不 rollback。
- Consumes: Task 8 persistent saver 与 M3 `DurableWorkflowGraphApp`。
- Removes: Deep Research 的 `DurableWorkflowWorker`、claim/lease/CAS/ready-node execution authority；业务 SQLite 只留 submission/owner/artifact/audit/idempotency/query projection。

- [ ] **Step 1: 记录 Gate P2 并写 RED**

Gate 必须附跨 host recovery 命令结果、已签核 manifest 和 operator cutover 批准。manifest 对现存 `legacy_scheduler_v2` 状态固定如下，不能在 route 中临时猜测：

| legacy status | cutover 处理 | rollback 窗口 |
| --- | --- | --- |
| `completed/failed/cancelled` | 保留只读业务记录/result/audit，不创建 graph checkpoint，不迁移 engine | 无执行路径，仅保持查询 |
| `queued` 且没有 started attempt/artifact/side-effect reservation | 进入 `migration_prepared -> graph checkpoint -> migration_committed` 两阶段状态机 | 仅 prepared 且确认无 checkpoint 时可回退；已有 checkpoint 后禁止转回 legacy |
| `running/recovering` | 进入 drain allowlist；cutoff 后旧 worker 只能续租/提交 manifest 列出的在途 workflow，不能 claim 新 workflow；到 deadline 未终结则 operator 明确 `cancelled` 或 `failed` 并写 reason，禁止中途转 graph | legacy code/worker 保留到该集合归零且 rollback deadline 关闭 |
| `waiting_input/blocked` | 继续由 legacy resume route 与 worker 完成，或由 owner/operator 显式取消；没有等价 native interrupt checkpoint 时禁止自动迁移 | legacy resume route 保留到集合归零 |

rollback 必须由 operator 原子把 manifest 从 `cutover_active` 切为 `rollback_requested`，该 transition 先停止 GraphHost 接受新 submission；只有此 phase 且在 `rollback_deadline` 前，才可把**尚未产生任何 graph checkpoint**的 prepared submission 路由回 legacy。`cutover_active` 即使 rollback window 尚未到期也只重试 ensure；任何已有 checkpoint/publish reservation 的 workflow 必须继续 GraphHost drain。删除 legacy 代码的硬前置仍是非终态/lease/waiting 全零、phase=`retired`、deadline 关闭、retirement audit 存在。

业务 SQLite 与 LangGraph checkpointer 不共享事务。pristine queued 固定采用：业务事务 A 以 revision/CAS 锁定 record，写 `migration_prepared`（stable thread、migration idempotency key、source revision、manifest digest）并冻结 legacy claim，engine 暂不切换；事务外调用 `WorkflowGraphHost.ensure_started(thread_id, idempotency_key)` 幂等检查/创建首 checkpoint；业务事务 B 确认 checkpoint 匹配 workflow/schema 后，以 prepared revision/CAS 写 `migration_committed` 并切 engine。`WorkflowMigrationReconciler` 扫描 prepared：已有 checkpoint只重试事务 B；无 checkpoint且 phase=`rollback_requested`、deadline 未过时可 CAS rollback；无 checkpoint且 phase=`cutover_active|draining` 时只重试幂等 ensure。并发 reconciler 每次按 manifest revision CAS，phase 改变后重新读，不用过期决定。

随后测试：

```python
async def test_deep_research_route_uses_graph_host_and_never_claims_legacy(monkeypatch, app):
    monkeypatch.setattr(legacy_store, "claim_ready_work_item", forbidden)
    handle = await app.start_deep_research(owner_request)
    assert handle.execution_engine == "langgraph_v3"

def test_cutover_manifest_classifies_every_legacy_status(store, manifest):
    decisions = WorkflowCutoverController(store, manifest).inventory()
    assert decisions.counts == {
        "terminal_read_only": 3,
        "migrate_pristine_queued": 1,
        "drain_running": 2,
        "drain_waiting": 2,
    }

@pytest.mark.parametrize("crash_point", ["after_prepare", "after_checkpoint", "before_commit"])
async def test_queued_migration_reconciles_every_crash_point(cutover_probe, crash_point):
    await cutover_probe.migrate_with_crash(crash_point)
    await cutover_probe.rebuild_and_reconcile()
    assert cutover_probe.graph_start_count == 1
    assert cutover_probe.record.execution_engine == "langgraph_v3"
    assert cutover_probe.events[-1].type == "migration_committed"

@pytest.mark.parametrize(
    ("phase", "expected"),
    [("cutover_active", "ensure_started"), ("rollback_requested", "migration_rolled_back"), ("draining", "ensure_started")],
)
async def test_reconciler_phase_is_mutually_exclusive(cutover_probe, phase, expected):
    assert await cutover_probe.reconcile_prepared_without_checkpoint(phase) == expected
```

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_workflow_graph_host.py tests/tdd/native-langgraph-m5/test_workflow_cutover_manifest.py tests/tdd/native-langgraph-m3/test_no_deep_research_scheduler.py`

Expected: FAIL，production composition 仍启动 legacy worker。

- [ ] **Step 3: 切换真实 composition，保持薄产品投影**

```python
app.state.workflow_graph_host = await WorkflowGraphHost.open(config, runtime_services)
# routes_workflows.py only calls host methods after identity validation.
```

状态/events/result facade 从 graph snapshot 与业务 committed facts 投影；不得暴露 native state/task/checkpoint。`WorkflowCutoverController` 先冻结新 legacy claim，按事务 A/幂等 host/事务 B 迁移 pristine queued，分别 drain running/recovering 与 waiting/blocked，并输出 owner/status/lease 数量而不含用户正文。SQLite 测试覆盖双 controller 并发、三个 crash point、重复 ensure、checkpoint 后拒绝 rollback、window 内无 checkpoint 的安全回退。只有上述 retirement 前置全部成立后，才删除 legacy execution；否则停在 cutover/drain commit。

- [ ] **Step 4: GREEN、重启恢复与 scheduler-negative mutation（cutover/drain，不删除 legacy）**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_workflow_graph_host.py tests/tdd/native-langgraph-m5/test_workflow_cutover_manifest.py tests/tdd/native-langgraph-m3 tests/core/contract/test_gateway_contract.py`

Run: `rg -n 'DurableWorkflowWorker|claim_ready_work_item|renew_work_item_lease|next_ready_work_item|legacy_scheduler_v2' src/assistant_agent/api src/assistant_agent/runtime src/assistant_agent/workflows`

Expected: pytest PASS；重启 controller 不重复迁移 queued，不会把已有 checkpoint 的 graph record 回滚给 legacy；deadline 分类与 retirement precondition 可确定复算。此时 legacy worker/runtime/execution 仍存在且只服务 manifest drain allowlist；临时恢复一个非-drain claim call 时 negative test 必须失败。

- [ ] **Step 5: 提交可回滚的 cutover/drain 阶段**

```bash
git add src/assistant_agent/workflows/graph_host.py src/assistant_agent/workflows/cutover.py src/assistant_agent/workflows/service.py src/assistant_agent/workflows/store.py src/assistant_agent/workflows/sqlite_store.py src/assistant_agent/workflows/models.py src/assistant_agent/api/app.py src/assistant_agent/api/routes_workflows.py src/assistant_agent/runtime/runtime.py tests/tdd/native-langgraph-m3/test_no_deep_research_scheduler.py tests/tdd/native-langgraph-m5/test_workflow_graph_host.py tests/tdd/native-langgraph-m5/test_workflow_cutover_manifest.py docs/tool-calling-architecture.md
git commit -m "feat: cut over workflows with legacy drain"
```

- [ ] **Step 6: 以机器查询关闭 retirement gate**

```python
retirement = controller.retirement_status(now=approved_now)
assert retirement.nonterminal_legacy_count == 0
assert retirement.active_legacy_lease_count == 0
assert retirement.waiting_legacy_count == 0
assert retirement.manifest_phase == "retired"
assert retirement.rollback_closed is True
assert retirement.retirement_audit_present is True
```

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_workflow_cutover_manifest.py -k retirement`

Expected: PASS；任一计数/manifest/audit 条件不满足都 fail closed，且不得进入 Step 7。

- [ ] **Step 7: 删除 legacy execution 并单独 GREEN/commit**

删除 `src/assistant_agent/workflows/worker.py`、`runtime.py`、`execution.py`，同时移除 `api/app.py` 的旧 worker lifecycle 与 `runtime.py` 的 `run_work_item`。业务 store 只保留 terminal legacy 只读查询与版本化 migration audit reader。

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_workflow_graph_host.py tests/tdd/native-langgraph-m5/test_workflow_cutover_manifest.py tests/tdd/native-langgraph-m3/test_no_deep_research_scheduler.py tests/core/contract/test_gateway_contract.py`

Run: `rg -n 'DurableWorkflowWorker|claim_ready_work_item|renew_work_item_lease|next_ready_work_item|legacy_scheduler_v2' src/assistant_agent/api src/assistant_agent/runtime src/assistant_agent/workflows`

Expected: pytest PASS；`rg` 只允许 `models.py/store.py/sqlite_store.py/cutover.py` 的历史 schema、terminal reader 和 migration audit，零 legacy execution call site。

```bash
git add -A src/assistant_agent/workflows src/assistant_agent/api/app.py src/assistant_agent/runtime/runtime.py tests/tdd/native-langgraph-m5 tests/tdd/native-langgraph-m3/test_no_deep_research_scheduler.py
git commit -m "refactor: retire legacy workflow scheduler"
```

### Task 10: Gate P3——LangSmith Release Review 与 Runtime Regression 等价验收

**Files:**
- Modify: `evals/release_review/cli.py`
- Modify: `evals/release_review/experiment.py`
- Modify: `evals/release_review/report.py`
- Modify: `evals/release_review/service.py`
- Create: `evals/release_review/langsmith_backend.py`
- Modify: `evals/langsmith_runtime_regression/experiment.py`
- Modify: `evals/langsmith_workflow_regression/experiment.py`
- Modify: `scripts/run_release_review.py`
- Modify: `scripts/run_langsmith_runtime_regressions.py`
- Modify: `scripts/run_langsmith_workflow_regressions.py`
- Create: `tests/tdd/native-langgraph-m5/test_langsmith_equivalence_gate.py`
- Modify: `evals/README.md`

**Interfaces:**
- Produces: `LangSmithEquivalenceReport`，分别记录 Release Review、Runtime Regression、Workflow Regression 的 dataset/project/experiment ID、active examples、root runs、required Feedback、native tree audit 与 infrastructure failures。
- Consumes: 实际 compiled graph target；保留 Git Release scenarios、Decision backend、Staging 隔离资源和既有评分语义。
- Replaces in `release_review/service.py`: Langfuse `sync_release_dataset/audit_release_scores/flush` 改为 `sync_langsmith_examples/audit_langsmith_feedback/wait_for_langsmith_runs`；service 不再 import `langfuse_backend.py` 或 `sync_dataset.py`。
- Gate rule: 三类 run 都 `complete=True` 且零 infrastructure failure 才写 `langsmith_equivalence=approved`。

- [ ] **Step 1: 写离线 gate 聚合 RED**

```python
def test_equivalence_requires_all_native_trees_and_feedback():
    report = LangSmithEquivalenceReport(
        release_review=passed_evidence(),
        runtime_regression=passed_evidence(),
        workflow_regression=missing_feedback_evidence(),
    )
    assert report.approved is False
    assert report.blockers == ("workflow_regression_feedback_incomplete",)
```

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_langsmith_equivalence_gate.py tests/tdd/langsmith-parallel-evaluation tests/tdd/native-langgraph-m3/test_langsmith_workflow_experiment.py`

Expected: FAIL，Release Review 仍绑定 Langfuse 且无统一 gate report。

- [ ] **Step 3: 迁移 runner 到 LangSmith actual graph target**

Release Review 每个 item 必须出现 `task -> AssistantTurnGraph -> assistant -> llm.chat`，Tool 位于 `execute_tool` 子树；Workflow 必须出现真实父图/subgraph/Send/join/verifier/repair。三个 runner 都通过 LangSmith API 分页核实 `parent_run_id/trace_id/reference_example_id` 与 required Feedback，不接受 SDK 内存结果或 astream event 代替远端 run。

- [ ] **Step 4: 离线 GREEN 后执行 operator Gate P3**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_langsmith_equivalence_gate.py tests/tdd/langsmith-parallel-evaluation tests/tdd/native-langgraph-m3/test_langsmith_workflow_experiment.py`

Operator-only commands（全部需 `MULTIMODAL_AGENT_PROVIDER_MODE=real`、未跟踪凭据和显式副作用开关；运行名固定由 release id `native-langgraph-m5-20260813` 派生，避免人工占位）：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_release_review.py --run --release-id native-langgraph-m5-20260813 --allow-real-provider --allow-staging-side-effects
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_langsmith_runtime_regressions.py --run --run-name native-langgraph-m5-20260813-runtime --allow-real-provider --allow-runtime-side-effects
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_langsmith_workflow_regressions.py --run --run-name native-langgraph-m5-20260813-workflow --allow-real-provider --allow-runtime-side-effects
```

Expected: 每项远端完整性 PASS 并形成可审计 ID；任一缺失则 P3 保持 BLOCKED，不能进入 Task 11。

- [ ] **Step 5: Commit**

```bash
git add evals/release_review evals/langsmith_runtime_regression evals/langsmith_workflow_regression scripts/run_release_review.py scripts/run_langsmith_runtime_regressions.py scripts/run_langsmith_workflow_regressions.py tests/tdd/native-langgraph-m5/test_langsmith_equivalence_gate.py evals/README.md
git commit -m "feat: complete langsmith evaluation equivalence"
```

### Task 11: Gate P4——删除 Langfuse runner/webhook/exporter/config/docs/deps 与 canonical shadow

**Files:**
- Delete: `evals/runtime_regression/`
- Delete: `evals/release_review/langfuse_backend.py`
- Delete: `evals/release_review/sync_dataset.py`
- Delete: `scripts/run_runtime_regressions.py`
- Delete: `scripts/run_langfuse.py`
- Delete: `deploy/langfuse_eval_webhook/`
- Delete: `src/assistant_agent/api/routes_eval_experiments.py`
- Delete: `src/assistant_agent/evaluation/release_review.py`
- Delete: `src/assistant_agent/evaluation/runtime_regression.py`
- Delete: `src/assistant_agent/observability/langfuse_config.py`
- Delete: `src/assistant_agent/observability/langfuse_scores.py`
- Delete: `src/assistant_agent/observability/runtime_audit/langfuse_source.py`
- Delete: `src/assistant_agent/observability/runtime_audit/online_evaluators.py`
- Delete: `src/assistant_agent/observability/workflow_otel.py`
- Delete: `src/assistant_agent/observability/workflow_trace.py`
- Delete: `src/assistant_agent/workflows/observed_store.py`
- Modify: `src/assistant_agent/api/app.py`
- Modify: `src/assistant_agent/observability/otel_exporter.py`
- Modify: `src/assistant_agent/observability/otel_mapping.py`
- Modify: `src/assistant_agent/observability/trace_persistence.py`
- Create: `src/assistant_agent/observability/runtime_audit/langsmith_source.py`
- Modify: `src/assistant_agent/observability/runtime_audit/cli.py`
- Modify: `src/assistant_agent/observability/runtime_audit/codex_input.py`
- Modify: `src/assistant_agent/observability/runtime_audit/collector.py`
- Modify: `src/assistant_agent/observability/runtime_audit/daily_runner.py`
- Modify: `src/assistant_agent/observability/runtime_audit/models.py`
- Modify: `src/assistant_agent/observability/runtime_audit/report.py`
- Modify: `src/assistant_agent/observability/runtime_audit/__init__.py`
- Modify: `src/assistant_agent/observability/runtime_audit/bundle_compaction.py`
- Modify: `src/assistant_agent/observability/runtime_audit/runner.py`
- Modify: `src/assistant_agent/observability/turn_evaluator.py`
- Modify: `src/assistant_agent/evaluation/experiment_runtime.py`
- Modify: `src/assistant_agent/evaluation/experiment_trace.py`
- Modify: `src/assistant_agent/evaluation/remote_run_control.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/startup_dependencies.py`
- Modify: `src/assistant_agent/runtime/server_startup_summary.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `docs/authority.toml`
- Modify: `docs/observability-harness.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `evals/README.md`
- Modify: `scripts/README.md`
- Modify: `README.md`
- Modify: `docs/context_engineering_status.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/observability-diagnosis-runbook.md`
- Modify: `scripts/run_runtime_audit.py`
- Modify: `scripts/run_server.py`
- Modify: `deploy/systemd/user/assistant-agent-runtime-audit.service`
- Create: `tests/tdd/native-langgraph-m5/test_langfuse_removed.py`
- Modify: `tests/tdd/runtime_audit/test_bundle_compaction.py`
- Modify: `tests/tdd/runtime_audit/test_daily_runtime_audit.py`
- Modify: `tests/tdd/runtime_audit/test_evaluator_metadata_projection.py`
- Modify: `tests/tdd/runtime_audit/test_final_review_fix.py`
- Modify: `tests/tdd/runtime_audit/test_runtime_audit.py`
- Modify: `tests/core/contract/test_observability_contract.py`
- Delete: `tests/tdd/release-review-native-experiment/test_release_review_webhook.py`
- Delete: `tests/tdd/release-review-native-experiment/test_release_trace_completeness.py`
- Modify: `tests/tdd/release-review-native-experiment/test_native_release_experiment.py`
- Modify: `tests/tdd/release-review-native-experiment/test_release_service.py`
- Modify: `tests/tdd/release-review-native-experiment/test_dataset_sync.py`
- Modify: `tests/tdd/release-review-native-experiment/test_initial_scenarios.py`
- Delete: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_cli.py`
- Delete: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_experiment.py`
- Delete: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_webhook.py`
- Delete: `tests/tdd/runtime-eval-feedback-loop/test_runtime_regression_webhook_proxy.py`
- Modify: `tests/tdd/runtime-eval-feedback-loop/test_experiment_runtime_host.py`
- Modify: `tests/tdd/langsmith-parallel-evaluation/test_dual_trace_export.py`
- Modify: `tests/tdd/langsmith-parallel-evaluation/test_langsmith_projection.py`
- Modify: `tests/tdd/mem0-langfuse-visualization/test_memory_otel_projection.py`
- Modify: `tests/tdd/vlm-input-overlay/test_vlm_input_overlay.py`
- Modify: `tests/tdd/vlm-observability/test_vlm_observability.py`
- Modify: `tests/tdd/vlm-trace-correlation-content/test_live_view_trace_link.py`
- Modify: `tests/tdd/vlm-trace-correlation-content/test_vlm_trace_content.py`

**Interfaces:**
- Consumes: Task 10 `approved` 证据与 operator P4 retirement approval。
- Removes: Langfuse SDK、Score writer、Dataset runner、signed webhook、local supervisor、Langfuse-specific OTel attributes/config/startup probes/docs。
- Removes: canonical Workflow commit → OTel shadow graph；LangSmith 只观察实际 compiled graph。
- Preserves: platform-neutral canonical business events/local trace/audit，以及真正通用的 OTLP exporter（去掉 Langfuse default endpoint/ID mapping）。
- Produces: `RuntimeAuditSource(Protocol).list_traces(window_start, window_end) -> list[RemoteTraceSnapshot]` 与唯一实现 `LangSmithSdkAuditSource(client, project_name)`；模型字段统一改为 `RemoteTraceSnapshot/RemoteRunSnapshot/RemoteFeedbackSnapshot`、coverage 字段改为 `remote_source_available/remote_trace_count/missing_remote_trace_count`，稳定 finding code 改为 `remote_trace_read_failed/remote_trace_missing`。

- [ ] **Step 1: 记录 Gate P4 与写 removal RED**

```python
def test_production_roots_have_no_langfuse_surface(repo_root):
    forbidden = repository_matches(
        repo_root,
        r"langfuse|ASSISTANT_AGENT_LANGFUSE|LANGFUSE_",
        roots=("src", "evals", "scripts", "deploy", "docs", "README.md", ".env.example", "pyproject.toml"),
        exclude=("docs/superpowers",),
    )
    assert forbidden == []

def test_workflow_store_has_no_shadow_trace_observer():
    source = inspect.getsource(AgentGraphRuntime.__init__)
    assert "ObservedWorkflowStore" not in source
    assert "create_workflow_otel_observer_from_env" not in source

def test_runtime_audit_uses_langsmith_source(fake_langsmith_client):
    source = LangSmithSdkAuditSource(fake_langsmith_client, project_name="assistant-agent")
    traces = source.list_traces(window_start=WINDOW_START, window_end=WINDOW_END)
    assert traces[0].trace_id == "trace-sentinel"
    assert traces[0].feedback[0].key == "assistant_agent.quality.response_quality"
```

允许的唯一历史命中为 `docs/superpowers/**` 与 `.superpowers/**`，它们不参与当前 Runtime/authority。

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_langfuse_removed.py`

Expected: FAIL，并输出当前消费者清单；若仍有 operator consumer，停止并保持 P4 BLOCKED。

- [ ] **Step 3: 一次性删除平台链路，不保留空 adapter**

从 `pyproject.toml` eval extra 删除 `langfuse>=4.10,<5`；删除 API router include、webhook launchers、startup probe/summary、score observer、Langfuse URL/trace-id 转换和 `langfuse.*` attributes。runtime audit 唯一远端 source 改为 `LangSmithSdkAuditSource`：按 project/window 分页读取 root run、真实 child run 与 Feedback，转换为 platform-neutral snapshot；`__init__/bundle_compaction/collector/cli/daily_runner/models/report/runner/codex_input` 与五个 runtime-audit 测试全部迁移中性字段/code。CLI `configure-evaluators` 调用 `configure_runtime_regression_evaluators(client, model_config_id, apply)`，新增必填 `--model-config-id`，不再保留 provider/model Langfuse evaluator flags。`turn_evaluator` 返回 platform-neutral metadata；evaluation experiment/trace/remote control、assistant loop nodes、server/runtime-audit scripts 和 systemd unit 删除 Langfuse webhook/URL/env/import。`collect/run` 只用 `create_langsmith_audit_source_from_env()`；本地 canonical ledger 仍是 completeness fallback，不保留 Langfuse fallback或 type alias。

测试 inventory 必须用 `git ls-files -co --exclude-standard` 联合 `rg --no-ignore -l -i`，并另运行 `rg --no-ignore -l 'sync_dataset|langfuse_backend|runtime_regression|routes_eval_experiments|langfuse_source|online_evaluators' tests` 检查删除模块的间接 import，不能只按平台字符串扫描。删除只保护 Langfuse webhook/runner 的 TDD；Release Review service/native Experiment、runtime host、LangSmith、memory/VLM tests 改成 LangSmith/platform-neutral assertions。`test_dataset_sync.py` 改测 `sync_langsmith_examples()` 的幂等 create/update/archive，`test_initial_scenarios.py` 保留 Git scenario schema/初始集合断言但从 `langsmith_backend` 装配 Example，不再 import `sync_dataset.py`。`test_observability_contract.py` 在本 Task 同步删除 Langfuse env/observer/attribute 断言，改守 `OBS-001` 的 actual graph/no shadow/platform-neutral exporter；Task 13 只再补最终矩阵断言，不延后修 collection failure。

- [ ] **Step 4: GREEN、import/依赖/文档扫描**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_langfuse_removed.py tests/core/contract/test_observability_contract.py tests/tdd/runtime_audit tests/tdd/release-review-native-experiment tests/tdd/runtime-eval-feedback-loop tests/tdd/langsmith-parallel-evaluation tests/tdd/mem0-langfuse-visualization tests/tdd/vlm-input-overlay tests/tdd/vlm-observability tests/tdd/vlm-trace-correlation-content`

Run: `rg -n -i 'langfuse|ASSISTANT_AGENT_LANGFUSE|LANGFUSE_' src evals scripts deploy docs/*.md README.md .env.example pyproject.toml`

Run: `git ls-files -co --exclude-standard -z | xargs -0 rg --no-ignore -l -i 'langfuse|ASSISTANT_AGENT_LANGFUSE|LANGFUSE_'`

Expected: pytest PASS；production-roots `rg` 零命中。`git ls-files` inventory 只允许 historical `docs/superpowers/**`、`.superpowers/**` 以及明确更名任务尚未执行的测试目录名；文件内容零 Langfuse import/env/attribute/assertion。真实 historical plans/specs 不改写。

- [ ] **Step 5: Commit**

```bash
git add -A src/assistant_agent evals scripts deploy pyproject.toml .env.example docs README.md tests/core/contract/test_observability_contract.py tests/tdd/native-langgraph-m5/test_langfuse_removed.py tests/tdd/runtime_audit tests/tdd/release-review-native-experiment tests/tdd/runtime-eval-feedback-loop tests/tdd/langsmith-parallel-evaluation tests/tdd/mem0-langfuse-visualization tests/tdd/vlm-input-overlay tests/tdd/vlm-observability tests/tdd/vlm-trace-correlation-content
git commit -m "refactor: retire langfuse and shadow tracing"
```

### Task 12: 收缩 AgentGraphRuntime 并删除无人依赖 API/兼容抽象

**Files:**
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/assistant_runtime_app.py`
- Modify: `src/assistant_agent/runtime/runtime_host.py`
- Modify: `src/assistant_agent/runtime/__init__.py`
- Keep unchanged: `src/assistant_agent/runtime/recovery.py`（`ToolExecutor` 与 `observability/langsmith_native.py` 的真实失败分类依赖）
- Keep unchanged: `src/assistant_agent/runtime/plan_validator.py`（独立 `automation/durable_tasks` 的 `DUR-001` 依赖）
- Keep unchanged: `src/assistant_agent/runtime/planning_models.py`（独立 durable task 与 `ToolExecutor` 依赖）
- Modify: `src/assistant_agent/runtime/legacy_tool_mapping.py`（重命名模块，去掉已退出 intent planner 的 legacy 命名）
- Create: `src/assistant_agent/runtime/tool_capability_mapping.py`
- Modify: `src/assistant_agent/runtime/tool_executor.py`
- Modify: `src/assistant_agent/observability/langsmith_native.py`
- Modify: `src/assistant_agent/automation/durable_tasks/hotel_price_watch.py`
- Modify: `src/assistant_agent/automation/durable_tasks/models.py`
- Modify: `src/assistant_agent/automation/durable_tasks/service.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_memory_lifecycle.py`
- Modify: `tests/core/integration/test_context_lifecycle.py`
- Modify: `tests/tdd/native-langgraph-m2/state_inventory.md`
- Modify: `tests/tdd/native-langgraph-m3/workflow_consumer_inventory.md`
- Create: `tests/tdd/native-langgraph-m5/test_runtime_surface.py`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`

**Interfaces:**
- Keeps on `AgentGraphRuntime`: `initialize_session_memory`（Agent-Service/Gateway）、`run_state`（MCP/multi-agent/system eval 和 sync compatibility）、`arun_state`、`astream_state`、`aresume_state`、`areplay_state`、`afork_state`、`drain_memory_ingestions`（Memory lifecycle）、`run_task_quantum`（独立 durable task）、`close`。
- Removes from `AgentGraphRuntime`: `run_work_item`（Task 9 删除 Workflow legacy consumer）与 `run`（仓库无真实调用方，`run_state` 已是兼容入口）。
- Keeps modules: `recovery.py/plan_validator.py/planning_models.py`，因为它们服务 Tool governance、LangSmith error classification 与独立 `automation/durable_tasks`，不是无人 API。
- Renames: `legacy_tool_mapping.py -> tool_capability_mapping.py`，`ToolExecutor` 改从新模块导入四个 mapping function，旧 module/export 直接删除，不保留 alias。
- Delegates: graph execution/history/time travel to compiled app；memory/tool/context/service preparation stays in focused collaborators。

- [ ] **Step 1: 生成 consumer inventory 与写 surface RED**

```python
def test_runtime_public_surface_is_intentional():
    assert public_methods(AgentGraphRuntime) == {
        "initialize_session_memory", "run_state", "arun_state", "astream_state",
        "aresume_state", "areplay_state", "afork_state",
        "drain_memory_ingestions", "run_task_quantum", "close",
    }
```

另测导入 `assistant_agent.runtime.legacy_tool_mapping` 失败、新 `tool_capability_mapping` 的结构化映射与 Tool governance 现有行为相同；`automation.durable_tasks` 继续 import `planning_models/plan_validator`，`ToolExecutor`/LangSmith 继续 import `recovery`。

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_runtime_surface.py`

Expected: FAIL，Runtime 仍含大量 compatibility surface。

- [ ] **Step 3: 逐项删除并把 composition 移入 owner**

删除 `run_work_item` 前先确认 Task 9 已删除 `workflows/execution.py`；删除 `run()` 后把依赖其源码的 core assertions 改为 `run_state/arun_state` 同义终态。把 `legacy_tool_mapping.py` 内容原样迁到 `tool_capability_mapping.py`，更新 `ToolExecutor` import 后删除旧模块。`recovery.py/plan_validator.py/planning_models.py` 保持实现不变，只运行真实消费者的测试证明未误删。不要新增 `LegacyRuntimeFacade` 或 deprecated alias。

- [ ] **Step 4: GREEN 与所有入口 smoke**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_runtime_surface.py tests/core/integration/test_runtime_lifecycle.py tests/core/integration/test_memory_lifecycle.py tests/core/integration/test_context_lifecycle.py tests/core/integration/test_durable_lifecycle.py tests/core/contract/test_tool_contract.py`

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent evals scripts`

Expected: PASS；API/Gateway/media/eval 都复用同一 compiled graph family，没有第二套 assistant loop。

- [ ] **Step 5: Commit**

```bash
git add -A src/assistant_agent/runtime src/assistant_agent/observability/langsmith_native.py src/assistant_agent/automation/durable_tasks tests/core/integration/test_runtime_lifecycle.py tests/core/integration/test_memory_lifecycle.py tests/core/integration/test_context_lifecycle.py tests/tdd/native-langgraph-m2/state_inventory.md tests/tdd/native-langgraph-m3/workflow_consumer_inventory.md tests/tdd/native-langgraph-m5/test_runtime_surface.py docs/runtime-event-stream-architecture.md docs/tool-calling-architecture.md
git commit -m "refactor: slim graph runtime facade"
```

### Task 13: 最终 Graph API 能力矩阵、authority 与验收

**Files:**
- Create: `tests/tdd/native-langgraph-m5/test_graph_api_matrix.py`
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/contract/test_observability_contract.py`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/observability-harness.md`
- Modify: `evals/README.md`
- Modify: `docs/authority.toml`
- Modify: `scripts/README.md`

**Interfaces:**
- Produces: machine-readable `GraphCapabilityEvidence` for every hard-constraint capability，字段 `capability/status/evidence_path/evidence_kind/gate`。
- Acceptance statuses: `implemented | not_applicable`；最终 M5 不允许 `partial/missing/blocked`。
- Consumes: Tasks 1–12 的 pytest、compiled topology、stream/checkpoint/history、operator Experiment evidence 和删除扫描。

- [ ] **Step 1: 写矩阵 RED**

```python
REQUIRED = {
    "StateGraph", "State", "Node", "Edge", "START", "END", "Conditional Edge",
    "Command", "Send", "Reducer", "Subgraph", "Pregel / Super-step", "Compile",
    "Invoke", "Stream", "Checkpoint", "Checkpointer", "Thread", "Interrupt", "Resume",
    "Memory", "Store", "Runtime Context", "Retry Policy", "Timeout", "Fallback",
    "Streaming Modes", "Time Travel", "Replay", "Fork",
}

def test_final_graph_api_matrix_is_complete(matrix):
    assert {item.capability for item in matrix} == REQUIRED
    assert not [item for item in matrix if item.status in {"partial", "missing", "blocked"}]
```

`Store` 可记 `not_applicable`，但证据必须是无 `runtime.store` consumer 且 compile 空参数已删除；`Memory` 证据必须指向 checkpointer/MemoryPluginHost 职责测试，而非机械接 Store。

- [ ] **Step 2: 运行矩阵 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5/test_graph_api_matrix.py`

Expected: 在 P1–P4 任一未通过时 FAIL，明确列出 blocked capability；不得改成 skip/xpass。

- [ ] **Step 3: 更新 authority 与 core invariant 决策**

逐条用源码/测试/远端 evidence 填矩阵。固定 core 决策：扩展现有 `LOOP-001` 与 `IDENT-001`，登记 safe opaque history、同 thread 新 invocation、native Replay/Fork re-entry；扩展 `OBS-001`，登记 LangSmith-only actual graph 与无 Langfuse/canonical shadow。`RUN-001/TOOL-001` 不改文字，因为终态与副作用 barrier 既有契约不变；不新建 feature-specific invariant。`test_runtime_lifecycle.py` 只保留一个最小 generic InMemory Replay/Fork identity case，`test_observability_contract.py` 只保留无影子 graph/platform-specific exporter 的结构化 contract；完整 feature 仍在可删除 M5 TDD。

- [ ] **Step 4: 全量最终验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-langgraph-m5 tests/tdd/native-langgraph-m2 tests/tdd/native-langgraph-m3
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check src/assistant_agent evals tests/tdd/native-langgraph-m5
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent evals scripts
git diff --check
```

Expected: 全部退出码 0；authority `valid=true/errors=[]`，所有 `review_required` owner 已书面复核；LangSmith P3 evidence 与 Langfuse P4 removal scan 均附最终报告。

- [ ] **Step 5: 最终 deletion 与产品协议审计**

Run: `rg -n -i 'langfuse|ObservedWorkflowStore|create_workflow_otel_observer|claim_ready_work_item|checkpoint_id|checkpoint_ns' src evals scripts deploy docs/*.md README.md .env.example pyproject.toml`

Expected: Langfuse/shadow/scheduler execution 零命中；`checkpoint_id/checkpoint_ns` 只允许内部 Graph app/negative safety test，产品 DTO/wire 零命中。

- [ ] **Step 6: Commit**

```bash
git add tests/tdd/native-langgraph-m5/test_graph_api_matrix.py tests/core docs evals/README.md scripts/README.md
git commit -m "docs: complete native langgraph m5 acceptance"
```

## 实施结束汇报格式

```text
M5 status: complete
Core invariant: LOOP-001, IDENT-001, OBS-001 changed as described in Task 13
Tests: tests/tdd/native-langgraph-m5 为临时 RED/GREEN，用户可手动整目录删除。
Provider: mock/local/offline；若执行 P3，另列真实 Provider/LangSmith 调用范围、Experiment IDs 与结果。
Graph API matrix: implemented=除 Store 外的全部硬约束；not_applicable=Store（无 runtime.store consumer，空 compile 参数已删除）；partial=[]; missing=[]
Deletion: legacy scheduler=removed; Langfuse=removed; canonical shadow=removed; unused Runtime APIs=run, run_work_item, legacy_tool_mapping module
Wire safety: no native checkpoint/task/interrupt/subgraph identifiers exposed
```

若任一 Gate 未通过，改用：`M5 status: blocked`，并逐条列 `gate/reason/evidence`，不得输出 complete 模板。

只有 Gate P1–P4 均 PASS、Task 13 矩阵无 `partial/missing/blocked` 且删除扫描通过时，才允许报告 `M5 status: complete`。
