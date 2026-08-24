# Supervisor Todo Planning A-lite Experiment

- Scope: offline Graph semantics only
- Provider mode: mock
- LangChain tested version: 1.3.15
- LangGraph tested version: 1.2.11
- LangGraph checkpoint tested version: 4.1.1
- Production wiring: none
- Temporary tests: user may delete this whole directory manually

## Commands

以下命令均在隔离 worktree 的根目录执行，exit status 均为 0。

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  tests/tdd/supervisor-todo-planning-alite-experiment
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -c \
  "import importlib.metadata as m; print(m.version('langchain')); print(m.version('langgraph')); print(m.version('langgraph-checkpoint'))"
```

结果：`langchain=1.3.15`、`langgraph=1.2.11`、`langgraph-checkpoint=4.1.1`。

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/supervisor-todo-planning-alite-experiment
```

结果：`24 passed in 3.85s`。

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest --collect-only -q
```

结果：只收集 `tests/core/**` 下 55 个测试项；未收集本实验目录。

## Gate results

| Gate | 结果 | pytest item | 结构化证据 |
| --- | --- | --- | --- |
| G1 | PASS | `test_supervisor_action_classification`、`test_supervisor_action_rejects_ambiguous_calls`、`test_supervisor_controls_then_finishes_without_create_agent` | control、单 task、多 task、final 均有唯一分类；混合、未知、重复 task 和多个 `write_todos` 均 fail closed；final AIMessage 无 ToolCall 直接结束 |
| G2 | PASS | `test_completed_todo_and_result_are_monotonic`、`test_completed_todo_cannot_be_removed_or_downgraded`、`test_write_todos_runs_through_standard_tool_node` | `write_todos` 经标准 `ToolNode` 返回配对 ToolMessage；completed Todo 不可删除、降级或改写；pending 可替换 |
| G3 | PASS | `test_three_tasks_run_in_parallel_and_join_once` | A/B/C barrier 的最大并发度为 3；三个结果齐全；`join_count=1`；父 ToolMessage 与三个原 task call ID 一一对应 |
| G4 | PASS | `test_worker_uses_private_create_agent_tool_loop`、`test_graph_builds_exactly_one_worker_agent` | 每个实验 Graph 只构建一个 Worker `create_agent`；Worker 完成 `model → read_probe → ToolMessage → structured result`；父消息和兄弟上下文未进入 Worker 私有 payload |
| G5 | PASS | `test_blocked_c_can_retry_without_replaying_a_or_b`、`test_replan_replaces_pending_c_and_preserves_completed_results`、`test_supervisor_can_finish_after_blocked_c` | blocked 正常 join 且 Todo 保持 pending；retry 只重跑 C；C→D 后 A/B completed/result 保留；Supervisor 可基于 A/B 直接 finish |
| G6 | PASS | `test_pending_writes_resume_only_failed_worker` | 首次 A/B/C 各执行一次，C 抛 TimeoutError 且 `join_count=0`；同 thread `ainvoke(None)` 后 A/B 保持 1 次、C 变为 2 次，随后只 join 一次 |
| G7 | PASS | `test_parent_graph_contains_only_alite_runtime_roles`、`test_worker_create_agent_is_visible_in_native_subgraph_stream` | 父 Graph 包含 supervisor/controls/worker/join 且无 Planner/Scheduler/Finalizer/Recovery 节点；v2 `subgraphs=True` stream 在 `worker:*` namespace 下可见 Worker agent 的 model/tools 更新 |

## Decision

`proceed_to_production_plan`

G1–G7 全部通过；其中 G6 pending writes 一票否决项和 G7 原生可观察性均通过。可以进入独立的生产实施计划，但不得把本实验 spike 直接移动或重命名为生产代码。

## Limits

- 本实验没有修改或接线生产 `AssistantPlanningGraph`。
- 本实验没有验证真实 Provider、MCP、真实 Tool 能力或回答质量。
- 本实验没有验证外部副作用 replay；生产 write/dangerous Tool 仍依赖既有 HITL 和业务幂等。
- 本实验没有验证旧 planning checkpoint 与新 state schema 的迁移或拒绝策略。
- 流式观测测试使用完整保留 ToolCall chunk 的 deterministic fake；首次不完整 fake 丢失 `tool_call_chunks`，修正 fake 契约后 Graph/subgraph stream 通过。
- 结论只适用于上述记录的依赖版本；升级 LangGraph 后应重新执行整个目录。
