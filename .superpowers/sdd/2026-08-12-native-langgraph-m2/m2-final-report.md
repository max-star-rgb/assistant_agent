# 原生 LangGraph M2 离线验收报告

日期：2026-08-12

状态：**Task 1、3–8 离线结构验收完成；Task 2 与真实 operator 验收 pending。不得声明 M2 complete。**

## 1. 验收范围与事实边界

本报告严格对应：

- `docs/superpowers/specs/2026-08-12-native-langgraph-graph-engineering-design.md`；
- `docs/superpowers/plans/2026-08-12-native-langgraph-m2.md`；
- 用户补充的 Graph API 原生开发硬约束。

本轮只使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`、本地 `InMemorySaver`、stdlib SQLite operation
ledger 和 scripted Provider；未联网、未调用真实 Provider、未运行真实 LangSmith Experiment/operator
样例。

## 2. 当前已证明的 M2 Graph API 事实

- `AssistantTurnGraph` 是真实 `StateGraph(AssistantTurnState, context_schema=GraphRuntimeContext)`，通过
  `START`、node、普通 edge、conditional edge、`END` 编译；普通产品主路消费 native `astream(v2)`，同步
  `invoke()` 只保留非 Gateway 兼容入口。
- checkpoint state 是版本化 strict JSON DTO；运行对象只经 Runtime Context 注入。真实
  `InMemorySaver` snapshot、state history、跨 Runtime/App 重建和同一 stable thread/new run resume 均有
  deterministic pytest 证据。
- `standard/planner/worker/verifier` 使用同一 node/edge 实现编译稳定 profile child；child 不自带 saver，
  由未来父图继承 checkpoint namespace。父子输入/输出通过窄 adapter，不传父图整份 state。
- 等待输入由真实 `await_input` node 调用 `interrupt()`；恢复由同一 thread 上的
  `Command(resume=...)` 进入 native stream。`waiting_user` 是非终态，不写 terminal history、Tool terminal
  hook 或 final response；resume 才产生唯一终态。
- `write|dangerous` Tool 在 backend 前使用 checkpointed stable operation scope 进入
  `SQLiteToolOperationStore` barrier；并发 owner、retry、resume、Runtime 重建和 ambiguous outcome 均
  fail closed，不重复触发副作用。
- node/LLM/Tool 的产品事实通过 native `custom` stream 进入唯一 `ProductEventProjector`；tasks、checkpoints、
  namespace 和完整 state 不进入现有产品协议，Runtime 不再模拟 graph node started/finished。
- Agent-Service、Gateway、HTTP 与媒体 composition root 保持 `allow_interrupt=False`，当前 wire 不增加
  waiting/resume；主 graph 路径不再使用 `invoke() + asyncio.to_thread/run_in_executor`。

本里程碑没有需要并行 state merge 的执行拓扑，因此不为覆盖名词机械加入 `Send`/Reducer/Pregel
super-step；这些是 M3 `DurableWorkflowGraph` fan-out/join 的验收核心。LangGraph Store、Retry Policy、
Timeout/Fallback 的通用运行职责以及 Time Travel/Replay/Fork 也不得由 M2 的局部接口冒充完成，分别随
后续图组合与 M5 收敛验收。

## 3. Core invariant 决策

- `RUN-001`：新增 `waiting_user` 可恢复非终态与 resume 后唯一终态；interrupted 不是 terminal。
- `LOOP-001`：新增 versioned checkpoint-safe state、稳定 profile graph family 和恢复 trajectory 契约。
- `IDENT-001`：删除 M1 “root 不启用 saver”临时说明；同 conversation invoke/resume 使用 stable
  `thread_id` 与不同 `run_id`。
- `TOOL-001`：新增 resumable write/dangerous operation barrier 与不重复 backend 副作用。
- `OBS-001`：新增 native graph/runtime fact 的单向产品投影，不模拟 graph node lifecycle。
- `GATE-001`：当前外部 wire 不暴露内部 `waiting_user`/resume，意外 waiting 走既有 error terminal。
- `DUR-001`：未改变；Workflow scheduler 的迁移属于 M3/M4。

新增/修改的永久断言仍只使用通用 Probe Tool、scripted adapter、`InMemorySaver`、本地 SQLite 和无语义
sentinel；没有引入具体 builtin Tool、Provider、prompt、profile 预算或第三方 saver snapshot 内部字段。

Mutation RED 已临时验证三类回退：profile child 丢失稳定 graph name、native interrupt 被误判 completed、
已提交 operation 被再次放行 backend；定向 core 结果为 `3 failed`。恢复实现后相同定向集合通过。

## 4. Saver 与依赖门

当前环境实际版本：

- `langgraph==1.2.4`；
- `langgraph-checkpoint==4.1.1`；
- `langsmith==0.10.18`；
- `langchain-core==1.4.3`；
- `langgraph-checkpoint-sqlite`：**未安装**。

Task 2 需要在用户明确允许安装依赖后，引入兼容的官方 async SQLite saver，并完成进程级
`AssistantRuntimeApp` owner、全部 async composition root、shutdown 顺序和跨 Runtime 重建恢复测试。当前
`none|memory` factory 只能证明 Graph API checkpoint/interrupt 语义，不能证明 production persistent
checkpointer，也不能用自研 saver 或 memory fallback 替代。因此本报告不把 M2 标记为 complete。

## 5. 删除/冻结门槛

- Runtime/API/Gateway 中不存在 `_emit_graph_execution_event`；旧 graph event enum/mapping 只保留外部兼容
  读取，不再由 Runtime 模拟发出。
- `src/assistant_agent` 中不存在自研 `CheckpointSaver`/`BaseCheckpointSaver` 实现。
- native source 可直接定位 `interrupt()`、`Command(resume=...)`、`subgraphs=True` 和 checkpoints streaming。
- service/Gateway 主 graph 路径的同步 request/thread bridge 已删除；Gateway pool checkout、WebSocket/媒体
  IO、sync-only Provider/Tool 等非 graph 边界按 owner contract 保留。
- checkpoint safety TDD 证明不保存 runtime object、credential、正文媒体、私有路径或任意 Tool body。
- write/dangerous Executor 路径缺 stable operation scope 时在 backend 前 fail closed。
- M1 LangSmith native graph tree 未被 ProductEventProjector 或 canonical OTel store 重建。

M2 不删除 Workflow v2 scheduler（M3/M4）、Langfuse（M5）、Gateway connection/delivery/cancel ownership、
媒体 API 与业务审计。

## 6. 验证证据

Task 8 提交前 fresh output：

- `pytest -q tests/tdd/native-langgraph-m2 tests/tdd/native-langgraph-runtime`：`204 passed`；
- `pytest -q tests/core/integration/test_runtime_lifecycle.py tests/core/contract/test_tool_contract.py tests/core/contract/test_gateway_contract.py`：`41 passed`；
- 默认 core `pytest -q`：`94 passed`；
- `pytest -q tests/tdd/native-langgraph-m2/test_no_graph_thread_bridge.py`：`3 passed`；
- authority validator：`valid=true`、`errors=[]`；`review_required` 已人工复核对应的 gateway、runtime、Tool
  与 test-policy owner，不要求制造额外文档 diff；
- documentation evidence collector 已执行 complete inventory 与 `7c3e5451..HEAD` 变更范围；历史
  `docs/interview/**`/历史 spec 的既存 missing/example link 不属于本次 current authority 变更；
- `compileall`、`git diff --check`、AST graph async thread-bridge gate、自研 saver/模拟 graph lifecycle
  搜索门槛全部通过。

`tests/tdd/native-langgraph-m2/` 与 `tests/tdd/native-langgraph-runtime/` 是临时 RED/GREEN feature 目录，用户
可以手动整目录删除；本轮不会替用户删除或把所有专项细节自动晋升 core。

## 7. 未完成的真实验收

- M1 的真实 LangSmith operator acceptance 仍 pending。
- M2 需要在 Task 2 完成后，对 standard、worker child、interrupt/resume 各运行一个真实样例，确认同一
  LangSmith trace/tree 中 graph/node/subgraph/LLM/governed Tool 层级、thread/run metadata 以及
  interrupted/completed 区分。
- 上述 operator evidence 和 official persistent saver evidence 缺一不可；离线 pytest 不能替代它们。

下一里程碑仍由 M3 接管 Workflow v2 DAG：以原生 `DurableWorkflowGraph`、`Send`、Reducer、Pregel
super-step、subgraph、checkpoint/resume 完成 Deep Research 纵切，并让旧 scheduler 对该 Workflow 类型退出。
