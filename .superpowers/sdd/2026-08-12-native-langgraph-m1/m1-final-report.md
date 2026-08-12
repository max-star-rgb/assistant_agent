# 原生 LangGraph M1 完成报告

## 完成内容

- `AssistantTurnGraph` 由每个 Runtime 稳定编译一次，并使用 LangGraph runtime context 注入运行依赖。
- Runtime 原生消费 LangGraph v2 async stream；Service、默认 HTTP Gateway 和 Agent-Service
  production roots 不再以 graph `to_thread` 作主路径。
- 同一 conversation 使用 stable `thread_id`，每次调用使用独立 `run_id`；M1 不伪造
  checkpoint namespace，root checkpointer 明确关闭。
- LangSmith 由 LangGraph/SDK 原生记录真实 graph/node/subgraph/LLM/governed Tool 层级；
  canonical audit 不再为 LangSmith 重建 OTel 影子树。
- LangSmith Runtime Regression 直接在 current Experiment `RunTree` 内 await 生产 native graph，
  并对 graph tree、run type、Example 身份和 Feedback 完整性 fail-closed。

## Core invariant

- 更新：`RUN-001`、`LOOP-001`、`IDENT-001`。
- 保持：`GATE-001` 外部协议行为。
- Task 7 已更新：`OBS-001` server canonical store 不为 LangSmith 重建 OTel tree。

## 验证摘要

- M1 native graph TDD：50 passed。
- LangSmith eval TDD：40 passed。
- Runtime/Context/Gateway/Observability 关联 core：51 passed。
- 默认 core：90 passed。
- documentation authority validator、compileall、`git diff --check` 通过。
- 删除门槛通过：graph service 主路无 `to_thread`，LangSmith Experiment 无 OTel
  binding/store，server 无 LangSmith OTel observer，每 Runtime 只构造一个 graph app。

`tests/tdd/native-langgraph-runtime` 与 `tests/tdd/langsmith-parallel-evaluation` 是临时 RED/GREEN
feature，用户可以日后手动整目录删除；本轮没有自动删除或全量晋升 core。

## 真实验收

未获授权，未运行真实 LangSmith `--inspect`/`--preflight`/`--run`，未运行真实
Provider，未访问网络或付费服务。因此 M1 的真实 Experiment UI tree 与 Feedback 落库仍是
operator 验收项，不在本报告中声称已通过。

## 后续边界

- M2：持久 checkpointer、最小可序列化 state、profile/subgraph、interrupt/resume。
- M3：Deep Research `DurableWorkflowGraph` 纵切、`Send` fan-out、reducer/join/repair/resume。
- M5：LangSmith Release Review 等价验收、Langfuse 与无消费者兼容 trace/eval/runtime 设施退出。

对主 spec 无未声明偏移；上述未完成项均是 spec 定义的后续里程碑。
