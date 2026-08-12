# M3 DurableWorkflowGraph state inventory

本表只描述 `DurableWorkflowGraph` v3 / state schema 1 的 checkpoint 事实。运行服务由 LangGraph Runtime Context 注入，不进入 state。

| channel / DTO | checkpoint 必需性 | 上限 | 恢复消费者 |
| --- | --- | --- | --- |
| `graph_name/graph_version/state_schema_version` | 必需；拒绝不兼容 checkpoint | 固定 `DurableWorkflowGraph/3/1` | graph app、state validator |
| `execution_engine` | 必需；graph 只接受 `langgraph_v3` | 固定 literal | graph app、legacy claim barrier |
| `workflow_id/workflow_type/identity` | 必需的 typed owner 与稳定 thread/turn-origin 事实；禁止从 workflow_id 反推 | user/session/agent/thread/turn-origin 单项 512；type 固定 `deep_research` | app、branch context factory、owner/thread fail-closed 校验 |
| `workflow_thread_id/invocation_run_id/invocation_trace_id` | thread 稳定；run/trace 属于当前 invoke/resume，不与 ingress turn 混用 | 单项 512 | checkpointer config、child invocation identity、trace metadata |
| `definition_version/current_plan_version` | admission 的可信版本输入 | definition 80；plan version >=1 | deterministic materializer |
| `submission` | 必需的 typed Deep Research 请求 | objective 10k；deliverables 32；constraints 64；questions 64；seed refs 128 | planning/admission |
| `admitted_plan` | admission 后必需 | nodes 256；dependencies 64/node；bindings 64/32 | wave router、join、verifier、repair |
| `status/phase` | 必需控制事实 | literal enum | conditional edges、product projection |
| `execution_generation_by_node` | admission 后完整覆盖所有 node | 256 nodes；generation 0..64 | current result 派生、repair |
| `active_wave` / `WorkflowProfileAssignment` | 当前 `Send` branch 的完整、可恢复 assignment | 256 branches；objective 10k；constraints 64；artifact refs 128；Tool names 256 | branch child adapter、parent join、interrupt mapping |
| `result_ledger` | append-only `(node_id,generation)` 结果 ledger；ACI reducer | 256×65 logical keys；每 slot 最多两个 digest variant | join、conflict fail-closed、repair/history |
| `resume_values_by_action_ref` | replay-safe typed resume ledger | 1,000 actions；32 fields/action；4k/value | Task 5 resume apply |
| `consumed_action_refs` | resume 消费幂等事实 | 1,000 sorted unique refs | Task 5 resume apply |
| `repair_round` | repair budget/终止事实 | 0..64 | verifier repair router |
| `budget` / assignment `budget_slice` | 父图剩余预算与 branch 预留 | 非负整数；deadline ISO-8601 | join 集中扣减、admission/policy |
| `result_artifact_refs` | 已发布/候选的稳定 artifact 引用 | 128 opaque refs | publish、产品结果投影 |
| `errors` | prompt-safe 结构化 graph failure facts | 256；message 2k | fail route、诊断投影 |
| `planner_assignment/planner_child_state/planner_result` | 仅 planning subgraph 使用；分别是完整 typed assignment、严格 namespaced `AssistantTurnState` 和 bounded proposal/usage | 沿用各 DTO 上限 | `AssistantTurnGraph.planner`、v2 parser、admission |

`WorkflowProfileAssignment.assignment_ref` 是全部 assignment 字段的 canonical SHA-256；`tool_scope_ref` 必须匹配当前 sealed `ToolRegistry.generation`。factory 只能用 outer assignment + child checkpoint + process-owned services 纯重建全新 `AgentState`、`ToolExecutor` 与 `GraphRuntimeContext`；可选 cache/pool 不能拥有 identity、assignment、Tool scope 或 mutable branch state。

明确禁止进入 checkpoint：Provider client/raw response/hidden reasoning，`ToolRegistry`、`ToolExecutor`、`ToolSpec` 实例、Tool raw result/argument body，数据库连接，`MemoryPluginHost`、artifact/memory/media 正文，filesystem 绝对路径，event sink/callback/stream writer/cancel token，API resume token/native interrupt ID，lease/CAS/heartbeat token，以及 credential/cookie/session token/signature/API key。业务 artifact 只保存 owner-bound opaque ref；LangGraph checkpoint 只拥有执行位置和严格 state。
