# Assistant Graph 原生状态收敛实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LangGraph 的原生 edge、checkpoint snapshot、interrupt 和 Runtime 成为 Assistant 执行事实源，逐步删除 `AssistantTurnState` 中重复的控制状态与 legacy 全量 hydrate/project 适配。

**Architecture:** 第一阶段先把普通执行、resume、replay 和 fork 从 `continuation -> time_travel_anchor -> prepare_invocation` 手写状态机迁移到原生 `StateSnapshot.next/tasks/interrupts`，旧 checkpoint 只保留显式兼容入口。第二阶段把 invocation/profile/预算等静态调用事实移入 `GraphRuntimeContext` 或 `Runtime.execution_info`。第三阶段将模型和工具轨迹收敛为增量 channel，删除 `AgentState` 与 `AssistantTurnState` 的整对象双向转换。每一阶段均保持 Tool 治理、Memory 冻结快照和副作用 ledger 不变。

**Tech Stack:** Python 3.11、LangGraph 1.2、LangChain Core、Pydantic v2、pytest、SQLite checkpointer。

## Global Constraints

- 默认使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 Provider。
- 所有本地 Tool 副作用仍必须经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- `memory_context` 继续作为 logical turn 的 checkpoint 冻结快照；replay/fork 不写长期 Memory。
- write/dangerous Tool 的 operation scope、contract digest 和 operation barrier 不得弱化。
- 保留用户现有 `AGENTS.md` 与 `docs/superpowers/plans/2026-08-13-langgraph-native-memory-nodes.md` 改动。
- 行为变化先在 `tests/tdd/langgraph-native-assistant-state/` 完成 RED/GREEN；只有已登记 invariant 变化才更新对应 core 测试。

---

### Task 1: 建立原生路由行为契约

**Files:**
- Create: `tests/tdd/langgraph-native-assistant-state/test_native_routing.py`
- Modify: `tests/core/integration/test_memory_lifecycle.py`
- Modify: `tests/core/INVARIANTS.md`

**Interfaces:**
- Consumes: `build_assistant_loop_graph(...) -> CompiledStateGraph`
- Produces: 原生拓扑契约：语义节点直接连接，checkpoint 的 `next` 是下一语义节点，不经过通用 anchor/gate。

- [ ] **Step 1: 写失败的拓扑测试**

```python
def test_assistant_graph_uses_native_semantic_edges() -> None:
    graph = build_assistant_loop_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert "time_travel_anchor" not in graph.nodes
    assert ("memory_recall", "assistant") in edges
    assert ("execute_tool", "assistant") in edges
    assert ("compose_response", "publish_response") in edges
    assert ("publish_response", "memory_commit") in edges
```

- [ ] **Step 2: 显式运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langgraph-native-assistant-state/test_native_routing.py
```

Expected: FAIL，原因是当前 graph 仍包含 `time_travel_anchor`，且语义节点没有直接 edge。

- [ ] **Step 3: 更新 LOOP-001/MEMORY-001 的稳定契约文字**

将 `LOOP-001` 明确为“原生 next/tasks/interrupts 是恢复事实源”，将 `MEMORY-001` 的节点顺序断言改成直接语义边；不增加新的 invariant ID。

- [ ] **Step 4: 保持测试为 RED，进入 Task 2 实现**

---

### Task 2: 用原生 edge 替代 continuation/anchor 调度

**Files:**
- Modify: `src/assistant_agent/runtime/assistant_loop_graph.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_state.py`
- Test: `tests/tdd/langgraph-native-assistant-state/test_native_routing.py`

**Interfaces:**
- Consumes: `route_after_assistant_turn_state(state) -> str`、`route_after_await_input_turn_state(state) -> str`
- Produces: `build_assistant_loop_graph()` 与 `build_namespaced_assistant_loop_graph()` 的直接 native topology。

- [ ] **Step 1: 删除 `_semantic_node` 对 `continuation` 的写入**

节点 wrapper 只校验并返回节点 update；路由分别注册到对应 conditional edge：

```python
graph.add_edge(START, "prepare_invocation")
graph.add_edge("prepare_invocation", "memory_recall")
graph.add_edge("memory_recall", "assistant")
graph.add_conditional_edges("assistant", route_after_assistant_turn_state, {
    "execute_tool": "execute_tool",
    "await_input": "await_input",
    "finish": "compose_response",
})
graph.add_conditional_edges("await_input", route_after_await_input_turn_state, {
    "execute_tool": "execute_tool",
    "assistant": "assistant",
})
graph.add_edge("execute_tool", "assistant")
graph.add_edge("compose_response", "publish_response")
graph.add_edge("publish_response", "memory_commit")
graph.add_edge("memory_commit", END)
```

- [ ] **Step 2: 让 resume 在 `await_input_node` 内完成 invocation re-entry**

在处理原生 `interrupt()` 返回值前，若 checkpoint run 与 `runtime.context.agent_state.run_id` 不同，则调用现有 `reenter_assistant_invocation(...)`；保持 owner/profile/ref 校验与 claim 不变。

- [ ] **Step 3: 兼容旧 v4 checkpoint**

本阶段保留 `continuation` schema 字段但不再用于新 graph 路由，标记为 deprecated compatibility data；新状态固定写 `None` 或稳定默认值，旧 checkpoint 通过迁移函数映射到原生下一节点选择器。

- [ ] **Step 4: 运行 Task 1 测试并确认 GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langgraph-native-assistant-state/test_native_routing.py
```

Expected: PASS。

---

### Task 3: 将 Replay/Fork 选择器改为 native snapshot

**Files:**
- Modify: `src/assistant_agent/runtime/assistant_graph_app.py`
- Modify: `src/assistant_agent/runtime/graph_time_travel.py`
- Create: `tests/tdd/langgraph-native-assistant-state/test_native_time_travel.py`

**Interfaces:**
- Consumes: `StateSnapshot.next`、`StateSnapshot.tasks`、`StateSnapshot.metadata`、`StateSnapshot.interrupts`
- Produces: `_history_summary(...)`、`aprepare_time_travel(...)`、`aexecute_time_travel(...)` 不读取 `state["continuation"]`。

- [ ] **Step 1: 写失败的 snapshot 选择测试**

```python
def test_history_uses_snapshot_next_without_continuation() -> None:
    state = valid_state_without_continuation()
    snapshot = snapshot_with(values=state, next=("execute_tool",))
    summary = summarize(snapshot)
    assert summary.next_nodes == ("execute_tool",)
```

测试同时覆盖：pending native interrupt 只从 `tasks/interrupts` 判定；read Tool checkpoint 可 replay；write/dangerous 仍经过现有 effect guard。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langgraph-native-assistant-state/test_native_time_travel.py
```

Expected: FAIL，原因是当前 selector 只接受 `next == ("prepare_invocation",)` 并读取 `continuation`。

- [ ] **Step 3: 实现 native selector 和 fork update**

允许的下一节点集合来自 compiled graph 的语义节点；fork 使用 snapshot metadata 中唯一的 last writer 作为 `aupdate_state(..., as_node=last_writer)`，更新后验证返回 snapshot 的 `next` 与原 snapshot 一致。无法唯一确定或发生漂移时返回 `graph_checkpoint_not_replayable`，不得猜测。

- [ ] **Step 4: 运行 time-travel TDD 并确认 GREEN**

运行 Task 3 目录测试，预期全部 PASS。

---

### Task 4: 移出 invocation/profile/预算重复字段

**Files:**
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_state.py`
- Modify: `src/assistant_agent/runtime/assistant_graph_profiles.py`
- Modify: `src/assistant_agent/memory/backends/disabled.py`
- Modify: `src/assistant_agent/memory/backends/mem0.py`
- Modify: `src/assistant_agent/memory/backends/langmem.py`
- Create: `tests/tdd/langgraph-native-assistant-state/test_runtime_context_facts.py`

**Interfaces:**
- Consumes: `GraphRuntimeContext.invocation_kind`、`GraphRuntimeContext.graph_profile`、LangGraph `Runtime.execution_info`
- Produces: Memory 与节点策略只读取 native Runtime/context；checkpoint 仅保存恢复所需的 immutable policy digest。

- [ ] **Step 1: 写失败测试**

验证 state 缺少 `invocation_kind` 和 max-budget 字段时，resume/replay/fork Memory 策略及 Tool 分类预算仍由 `GraphRuntimeContext` 正确执行；profile mismatch 仍 fail closed。

- [ ] **Step 2: 运行并确认 RED**

当前 strict DTO 要求这些字段，测试应以 `assistant_state_invalid` 或缺失字段失败。

- [ ] **Step 3: 最小实现 Runtime facts**

新增 immutable `GraphExecutionPolicy`：

```python
@dataclass(frozen=True, slots=True)
class GraphExecutionPolicy:
    profile: AssistantGraphProfileName
    model_call_limit: int
    action_tool_call_limit: int
    control_tool_call_limit: int
    policy_digest: str
```

由 composition root 构造并放入 `GraphRuntimeContext`；节点计数保留在 state，最大值不再 checkpoint。若同一 graph 支持多个 profile，state 只保存 `policy_digest`；不同 graph identity 场景直接使用 `graph_id/profile child`。

- [ ] **Step 4: 运行 TDD 并确认 GREEN**

运行 Task 4 测试，预期全部 PASS。

---

### Task 5: 收敛 legacy trajectory 双表示

**Files:**
- Modify: `src/assistant_agent/runtime/assistant_graph_state.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Create: `src/assistant_agent/runtime/assistant_state_channels.py`
- Create: `tests/tdd/langgraph-native-assistant-state/test_incremental_channels.py`

**Interfaces:**
- Consumes: LangChain `AIMessage`/`ToolMessage`、LangGraph reducer、现有 prompt-safe Tool observation projector
- Produces: `AssistantStateChannels`，节点返回局部 update，不再全量构造临时 `AgentState`。

- [ ] **Step 1: 写失败的增量 channel 测试**

测试一个 scripted model -> governed Tool -> model -> compose 轨迹，断言：AI Tool Call 与 prompt-safe ToolMessage 只保存一份；Tool 仍经过 registry/executor；节点 update 不携带运行期 client/service。

- [ ] **Step 2: 运行并确认 RED**

当前实现仍通过 `assistant_loop_state_from_turn_state()` 和 `assistant_turn_state_from_loop_state()` 全量转换，测试应因重复 trajectory 或缺少增量 channel 失败。

- [ ] **Step 3: 实现最小 channel schema**

使用 `messages` reducer 保存模型/工具轨迹；保留 `pending_effects` 中的 operation scope、effect category、contract digest 和 bound-input digest。`memory_context`、产品 terminal、错误与副作用 outcome 保留独立 domain channel。

- [ ] **Step 4: 逐节点移除 legacy hydrate/project**

依次迁移 `assistant`、`execute_tool`、`compose_response`；每迁移一个节点运行 Task 5 测试，最后删除不再使用的全量转换入口。

- [ ] **Step 5: 运行 TDD 并确认 GREEN**

运行完整 `tests/tdd/langgraph-native-assistant-state`，预期全部 PASS。

---

### Task 6: 权威文档、核心回归与完成检查

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_memory_lifecycle.py`
- Modify: `tests/core/INVARIANTS.md`

**Interfaces:**
- Consumes: Task 1-5 的最终 Graph/state/runtime 契约
- Produces: 与源码一致的 authority、LOOP-001/RUN-001/MEMORY-001 回归证据。

- [ ] **Step 1: 更新 authority**

明确：native snapshot 是执行位置事实源；Runtime context 承载 invocation-local dependencies/policy；state 只保存动态业务恢复事实；自定义 node 是 LangGraph Graph API 的原生扩展点。

- [ ] **Step 2: 运行临时 TDD**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langgraph-native-assistant-state
```

- [ ] **Step 3: 运行受影响核心测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/integration/test_runtime_lifecycle.py tests/core/integration/test_memory_lifecycle.py tests/core/contract/test_tool_contract.py
```

- [ ] **Step 4: 运行文档权威检查**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
```

- [ ] **Step 5: 运行 diff 与静态检查**

```bash
git diff --check
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent/runtime src/assistant_agent/memory
```

- [ ] **Step 6: 独立代码审查**

使用 `superpowers:requesting-code-review` 检查 native API 使用、checkpoint migration、resume/time-travel 与副作用安全；修复发现后重新运行最小相关测试。

