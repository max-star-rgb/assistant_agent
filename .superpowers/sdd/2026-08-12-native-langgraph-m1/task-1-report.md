# Task 1 报告：稳定 Graph 身份、runtime context 与单次编译

状态：DONE

## 完成内容

- 新增 `GraphExecutionIdentity`：同一 `agent_id/user_id/session_id` 生成稳定 `thread_id`，每次 turn
  保留独立 `run_id` runnable config。M1 根图不启用 checkpointer，因此不声明虚拟 checkpoint namespace；
  持久 namespace/resume 属于 M2。
- 新增 `AssistantTurnGraphApp`，在 Runtime 构造期编译一次 `AssistantTurnGraph`，通过只读 `graph` 提供
  给后续 run 复用。
- `StateGraph` 使用 `GraphRuntimeContext` 作为 `context_schema`；每个 node 从 LangGraph `Runtime` 取得
  run-scoped context，注入后仍在返回前剥离 runtime-only state。
- `AgentGraphRuntime` 在 invoke 时传入 `context=runtime_context`，不再因请求而重新编译 graph。
- 修复审阅发现的 stable thread 串扰：每次初始 state 显式覆盖所有 run-scoped loop channel，第二个 turn
  不会把上一 turn 的 tool observation 回灌到 Provider 输入。

## RED/GREEN 证据

RED：

```text
ModuleNotFoundError: No module named 'assistant_agent.runtime.assistant_graph_app'
```

该 RED 来自：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_graph_app.py
```

审阅后补充的隔离回归也已先 RED：使用 `MemorySaver` 时，首 turn 的 `probe_tool` synthetic assistant/tool
消息出现在第二 turn Provider request（实际 role 列表为 `system,user,assistant,tool`）。

GREEN：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_graph_app.py
# 4 passed in 1.58s

MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/core/integration/test_runtime_lifecycle.py
# 15 passed in 1.59s

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/runtime/assistant_graph_app.py \
  src/assistant_agent/runtime/assistant_loop_graph.py \
  src/assistant_agent/runtime/graph_runtime.py \
  src/assistant_agent/runtime/runtime.py
# exit 0

git diff --check
# exit 0
```

## 测试决策

Core invariant: unchanged.

Tests: added/updated `tests/tdd/native-langgraph-runtime` for temporary RED/GREEN; user may delete the directory manually.

未修改 `tests/core`；该任务新增的是 M1 graph app 的临时实现验证，尚不构成已登记 core invariant 的长期结构化契约变更。

## 修改文件

- `src/assistant_agent/runtime/assistant_graph_app.py`
- `src/assistant_agent/runtime/assistant_loop_graph.py`
- `src/assistant_agent/runtime/graph_runtime.py`
- `src/assistant_agent/runtime/runtime.py`
- `tests/tdd/native-langgraph-runtime/test_graph_app.py`

## 初始 Commit

`b838f1f3c3c7394c8a03082894ab38cee1b89835` — `refactor(runtime): compile assistant graph once`

## Fix round 1：根图 checkpointer 裁决

主 spec 优先于 Task 1 brief 中关于根图 `checkpoint_ns` 的示例。审阅已证明当前 LangGraph 将根
`StateGraph` 的 `configurable.checkpoint_ns` 归一化为 `""`；它不是可用于每 turn 物理隔离的 namespace。
把 `run_id` 编入 `thread_id` 会破坏 stable conversation thread，也不符合主 spec。M1 本身不承诺持久化或
resume，它们属于 M2。

因此本轮修复：

- `AssistantTurnGraphApp` 的 M1 compiled graph 不再接收或挂载 checkpointer；`AgentGraphRuntime` 不再将
  任何 checkpointer 传入该 graph。
- `GraphExecutionIdentity` 只保留 stable `thread_id` 与 `run_id`；runnable config 不再声称有根图实际不会
  生效的 `checkpoint_ns`。
- 即便调用方为 M2/兼容目的传入 `MemorySaver`，M1 Runtime 也不写入它，从而不会产生同 session 的根 checkpoint
  污染、跨 run restore 或自定义 state 反序列化。
- TDD 改为消费者可观察行为：顺序 run 与并发 run 均复用同一 compiled graph、完成独立 run，传入的 saver 保持
  空；首 turn Tool observation 不会进入第二 turn Provider request。

Fix round RED：调整后的 TDD 在旧实现中 4 failed、1 passed：identity 仍携带 `checkpoint_ns`，且两个顺序和一个
并发 Runtime run 均向 `MemorySaver` 写入 checkpoint。

Fix round GREEN：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-langgraph-runtime/test_graph_app.py
# 5 passed in 1.62s

MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/core/integration/test_runtime_lifecycle.py
# 15 passed in 1.73s

MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q
# 86 passed in 2.88s
```

Fix round commit：

`0c22617e5969ddaac60036c6c4dbb0a2ecf5787c` — `fix(runtime): defer assistant checkpoints to m2`

## 自审

- 已采用消费者可观察行为断言同一 Runtime 复用 graph、run context 不持久化，以及后续 turn 不继承上次的
  Tool observation；没有用 mock builder 调用次数作为主要正确性证据。
- M1 刻意不实现持久 checkpoint、resume 或物理 turn namespace。M2 必须在可嵌套 graph/checkpointer 架构中，
  用 LangGraph 实际支持的结构确定持久 state、namespace 与恢复策略；不得重新引入虚假的根图
  `checkpoint_ns` 合同。
