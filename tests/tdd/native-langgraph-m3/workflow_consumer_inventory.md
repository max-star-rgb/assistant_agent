# M3 Workflow 产品消费者 inventory

本清单只记录非历史、非 TDD 的 Agent-Service / media 真实消费者。它约束 M3 产品投影，不把当前旧
HTTP response 的全部字段自动升级为兼容承诺。

| 产品事实 | consumer / call site | 保护级别 | graph 产品投影 |
| --- | --- | --- | --- |
| `workflow://<workflow_id>` handle | `src/assistant_agent/api/agent_service_websocket.py:1501,1605`；`scripts/media_simulator.py:759-768` | hard-protected | `WorkflowHandle.output_ref`；Agent-Service `outputRefs` wire 不变 |
| status / phase / waiting action | `scripts/media_simulator.py:513-535,573-626` | hard-protected | strict `WorkflowHandle`、`WorkflowWaitingAction`；HTTP adapter 后续只做 DTO 序列化 |
| product progress | `scripts/media_simulator.py:540,699-704` | hard-protected | `WorkflowProductProgress`；客户端不再从 `plan.work_items` 推进或重建进度 |
| cursor events | `scripts/media_simulator.py:500-511,536-539` | hard-protected | `WorkflowProductEvent` + `next_cursor`；event 不含 native task/checkpoint/ns |
| final result content | `scripts/media_simulator.py:550-560` | hard-protected | identity-scoped result adapter 返回 artifact `content`；无 `result_summary` fallback |
| waiting-input token/value | `scripts/media_simulator.py:583-614` | hard-protected | stable business `action_ref` / resume token adapter；native interrupt ID 不出 wire |

下列当前旧字段没有上述真实 consumer，标记为 `unconsumed-breaking-cleanup-allowed`：response `plan`、
`work_items`、lease、attempt、revision、raw `WorkflowBundle`、`/cancel` route。它们在本次安全 prework 中
尚未从 production route 删除；Task 5 persistent saver gate 通过后的正式 Task 6 cutover 才能重写或删除
production API。

禁止投影字段：`checkpoint_id`、`checkpoint_ns`、native task/interrupt ID、subgraph namespace、完整 graph
state、Provider response、Tool raw body、scheduler lease/CAS。

## M5 legacy drain consumer gate

`src/assistant_agent/workflows/execution.py` 对 `AgentGraphRuntime.run_work_item()` 的调用是当前唯一保留的
legacy Runtime surface consumer。2026-08-13 的只读 operator 检查仍显示 legacy workflow
`running=1`、`waiting_input=1`，所以 Task 9 retirement gate 未满足；本清单将该方法标记为
`drain-gated-temporary`，不把它误报为已退休，也不允许新入口依赖它。

其余 Runtime consumer 已统一到 `run_state` / `arun_state` / `astream_state` 或 compiled app owner；仓库
没有 `AgentGraphRuntime.run()` 调用方。`run_work_item` 只能在非终态、active lease 与 waiting 全部归零且
manifest/rollback/audit gate 完整通过后，随 legacy workflow execution 一起退出。
