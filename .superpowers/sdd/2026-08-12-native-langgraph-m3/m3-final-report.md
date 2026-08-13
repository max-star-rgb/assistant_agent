# M3 离线预验收报告

日期：2026-08-13

状态：**offline implementation prework ready; M3 acceptance pending**。

本报告只记录当前 checkout 可证明的事实。它不表示 production Deep Research 已切换到
`DurableWorkflowGraph`，也不表示持久恢复或 LangSmith 真实 Experiment 已验收。

## 1. Task 与 commit

| Task | 当前状态 | commit |
| --- | --- | --- |
| 1 strict state / reducer / Runtime Context | 离线完成 | `6a62830d`、`4b07ed81` |
| 2 planner profile / v2 admission | 离线完成 | `ea704318`、`a670747d` |
| 3 Conditional Send / Pregel join / worker | 离线完成 | `1838a6cd`、`9232dd8b`、`af7d5eea` |
| 4 verifier / Command repair / native policy | 离线完成 | `a355f982`、`4846907b`、`88406dbf` |
| 5 interrupt/resume/publish | 仅 InMemory 部分完成 | `2674efaa`、`9592a74b`、`a3bbdbef` |
| 6 product projection / cutover | 仅 projection 与 consumer prework | `0f736692`、`9c994224`、`718d77fc`、`1497c57c` |
| 7 LangSmith Workflow eval | 仅离线 prework | `8a829875`、`c0809731`、`f1a65960`、`8e69f728` |
| 8 删除与最终验收 | 本报告对应离线预验收；删除未执行 | 本次提交 |

## 2. Graph API 离线事实

- `DurableWorkflowGraph` 是真实 `StateGraph`，包含稳定 `START`/`END`、普通 edge、conditional edge、
  planner/worker/verifier subgraph、`Send` fan-out、Pregel super-step join 和 `Command` 控制路由。
- strict `DurableWorkflowState` 使用版本化 checkpoint-safe DTO；result ledger reducer 对重放、顺序和
  parenthesization 保持 ACI，同 node/generation 的冲突形成显式 conflict fact。
- planner、worker、verifier 都复用 compiled `AssistantTurnGraph` profile；branch assignment、child state、
  `AgentState`、Tool executor 和 `GraphRuntimeContext` 按 branch 独立重建；各 branch 只共享 process-owned、
  immutable runtime services。Deep Research profile 的本地 Tool scope 为空。
- worker wave 只由 admitted static DAG 和 current-generation ledger 推导；native graph 模块不调用
  `claim_ready_work_item`、lease renewal、`run_claim`、`next_ready_work_item` 或 legacy `ThreadPoolExecutor`。
- verifier 使用 `Command` 路由最小 generation repair、publish 或 fail；retry、timeout 与 error fallback
  注册在 compiled node policy，不由项目手写重试循环模拟。
- 父图 `await_branch_input` 是 Workflow interrupt 的唯一 owner；resume 从 fresh snapshot 将业务
  `action_ref` 映射为 native interrupt ID，保持同 thread、使用新 run ID；native ID 不进入产品 DTO。
- publish 使用稳定 operation key、prepare/commit ledger 和幂等 publisher；strict projector 只接受已校验的
  product fact，不把 state、task、checkpoint、namespace、Provider raw response 或 Tool raw body 投影到 wire。
- LangSmith workflow target 直接运行 compiled graph；离线 tree completeness 以 persisted run 的真实
  parent/trace/example identity 为契约，不接受 astream 事件伪装成远端 run。

## 3. Mutation-sensitive 证据

- 临时移除 `workflow_matches_claim_scope()` 的 `legacy_scheduler_v2` 硬限制后，
  `test_graph_v3_record_is_never_in_legacy_claim_scope` 按预期失败；恢复 production 代码后定向集合通过。
- 临时让 native `prepare_wave` 调用 legacy `next_ready_work_item()` 后，新增 scheduler-negative runtime spy
  按预期失败；恢复 production 代码后通过。mutation 均未提交。

这两个检查分别证明 engine gate 和“native graph 不触碰 shadow scheduler”不是仅靠源文本成立。

## 4. 受保护边界

- `langgraph_v3` 即使出现在误配置的 engine/type allowlist 中，也不能被 legacy store claim。
- 现有 Agent-Service/Gateway core 契约保持原样；媒体 simulator 只从 strict `progress` 与 final
  `result.content` 读取产品结果，不从旧 plan/result summary 重建。
- `assistant_agent.automation.durable_tasks` 仍存在并由 `DUR-001` core 测试保护，本轮未删除或改写。
- Tool 调用仍由既有 `AssistantTurnGraph` 与 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`
  治理链执行；Workflow graph 没有直接调用具体 Tool 实现。

## 5. Core invariant 决策

Core invariant: unchanged。

M3 graph topology、具体 Deep Research definition、repair/publish/projector 仍处于 feature 纵切期，继续保留在
可由用户手动整目录删除的 `tests/tdd/native-langgraph-m3/`。production cutover 与 persistent saver 未完成前，
不把这些临时事实机械晋升为 `DUR-001`/`LOOP-001`。既有 core 继续保护 runtime、identity、Gateway wire、
Tool governance、observability 与独立 durable task 的长期不变量。

## 6. 明确未完成项

1. M2 Task 2 官方 async SQLite saver dependency、进程级 owner 和跨 Runtime recovery 未获授权/未完成。
2. `WorkflowGraphHost`、API/runtime composition root 与 production Deep Research cutover 未实现；当前
   `_start_deep_research_workflow`、`DurableWorkflowWorker`、claim/lease/CAS 仍服务现有产品。
3. 因第 2 项，旧 Deep Research scheduler、OTel Workflow 投影和 ready-node 路径不能在本轮删除；
   `long_horizon` 的 M4 sunset 也尚未开始。
4. LangSmith 真实 Dataset、Experiment、native tree 与四项 Feedback 尚无 operator 证据。
5. Time Travel、Replay、Fork 产品能力属于 M5；本轮只有 native state history/replay-safe reducer 基础。

上述任一项仍未关闭时，M3 都不得标记 complete。

## 7. 验证

以下命令均在本 worktree 根目录执行：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m3 tests/tdd/native-langgraph-m2 \
  tests/tdd/native-langgraph-runtime tests/tdd/deep-research-mode
# 325 passed in 208.04s

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
# 94 passed in 6.44s

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
# valid=true; errors=[]; review_required=["test-policy"]

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  tests/tdd/native-langgraph-m3/test_no_deep_research_scheduler.py \
  src/assistant_agent/workflows evals/langsmith_workflow_regression \
  scripts/run_langsmith_workflow_regressions.py

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/workflows evals/langsmith_workflow_regression \
  scripts/run_langsmith_workflow_regressions.py

git diff --check
```

`test-policy` owner 已按 `tests/README.md` 和 `tests/core/INVARIANTS.md` 复核；临时验收保留在 TDD，
core invariant 不变。上述 ruff、compileall 与 diff check 均以退出码 0 完成。scheduler source gate还以
`rg` 检查 native
graph/planning/projection/publish 模块对
`DurableWorkflowWorker|ThreadPoolExecutor|claim_ready_work_item|claim_ready_item_in_bundle|renew_work_item_lease|run_claim|next_ready_work_item`
零命中；protected durable task 的 source/test 引用仍存在。review 修正后另行重跑 scheduler-negative 与
planning 定向集合。

全部验证使用 mock/local/offline；未调用真实 Provider、未联网、未写远端 LangSmith/Langfuse。
