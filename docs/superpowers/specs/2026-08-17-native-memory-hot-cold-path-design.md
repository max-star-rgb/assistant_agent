# 原生 Memory 热路径与冷路径设计

## 目标

将长期记忆按执行生命周期拆成两个原生 Graph：主 Assistant Graph 在每个顶层 chat run 中只召回一次并冻结快照；独立 Memory Graph 在 conversation 静默 30 分钟后执行提取与合并。记忆提取不进入用户回答关键路径，不使用进程内 timer、队列或 `LocalReflectionExecutor`。

## 决策

- 采用 LangChain 官方 `memory-template` 的 Agent Server SDK 调度方式，不使用 `RemoteReflectionExecutor` 固定的 `rollback` 策略。
- 主 graph 与 memory graph 作为两个 graph 注册到同一个 Agent Server。
- delayed memory run 与 chat run 使用同一个 conversation `thread_id`，由 Agent Server 保存 shared thread state。
- `after_seconds` 默认 1800 秒，使用 `multitask_strategy="enqueue"`；项目在每个新 chat run 开始时用官方 SDK 精确 rollback 旧 pending Memory run，并在最新回答后重新 enqueue。
- 用户身份只来自 Agent Server authenticated runtime；不把 `user_id` 暴露为可伪造 graph input 或 configurable 参数。

## 最终拓扑

```text
assistant-native-v1

START
  -> cancel_pending_memory_extractions
  -> memory_recall
  -> execution_router
       -> fast_agent ------------------+
       -> planning_graph               |
            -> planner                 |
            -> workers                 | frozen memory_context
            -> finalize                |
  -> enqueue_memory_extraction --------+
  -> END


assistant-memory-v1

START
  -> memory_extract
  -> END
```

`Memory` 仍是领域、目录和 backend protocol 边界，但不再表现为主图中一个已编译的 `AssistantMemoryGraph`。主图直接组合 `memory_recall` 节点；独立 memory graph 直接组合 `memory_extract` 节点。不存在从 compiled memory subgraph 中提取节点的概念。

## 热路径

每个顶层 chat run 进入 `memory_recall` 一次。节点使用 authenticated identity、当前 thread/run、标准 messages 与 Agent Server Store 调用 `MemoryBackend.recall`，将有界结果写入 `memory_context` 和 `memory_status`。

fast agent 的 `dynamic_prompt` middleware 只读取已经冻结的快照。planning graph 及其所有 worker 接收同一快照；worker 对共享 fast agent 的内部 invocation 不再触发 recall。

recall 使用 LangGraph 原生 `RetryPolicy`。重试耗尽后使用原生 error handler 写入 degraded 空快照并继续回答。

## 冷路径与 debounce

每个 chat run 开始时，`cancel_pending_memory_extractions` 通过官方 SDK 查询同 thread 的 pending runs，只对带 `assistant_agent_run_kind=memory_extraction` metadata 的 run 调用 `cancel(..., wait=True, action="rollback")`。fast 或 planning 生成最终 `AIMessage` 后，主图进入 `enqueue_memory_extraction` 并创建 delayed run：

```text
thread_id = 当前 conversation thread
assistant_id = assistant-memory-v1
input.messages = 当前完整 conversation snapshot
after_seconds = 1800
multitask_strategy = enqueue
metadata.assistant_agent_run_kind = memory_extraction
```

节点只等待 Agent Server 接受调度，不等待 memory LLM。调度失败属于辅助能力降级：使用原生 retry/error handler 结束主 run，已经生成的回答保持有效。

静默窗口到期后，`assistant-memory-v1` 在独立 server run 中调用同一个配置下的 `MemoryBackend.commit`。LangMem backend 执行 extract/consolidate，Mem0 backend 走其 adapter，disabled backend no-op。memory graph 的失败只影响该后台 run。

## 状态与公开接口

- 删除公开 `run_type=memory_extract`；调用方只提交 messages 与 `execution_mode`。
- 主 state 只保留 `memory_context`、`memory_status`；调度结果由原生 node/run trace 观测，不增加重复状态字段。
- memory graph 输入只接受标准 messages，身份与 thread/run 从 runtime 获得。
- 两个 graph 使用兼容的 messages channel，因此同一 thread 的 checkpoint state 可以被 memory graph 消费。

## 资源生命周期

进程级 composition 继续只创建一次模型、Tool、Memory backend 和编译图。主 graph 与 memory graph 共享同一个 process owner 及 backend，不重复发现 MCP。Agent Server graph factory 分别返回两个已编译 graph；进程 shutdown 统一关闭 owner。

## 验证

- 图结构：主图直接包含 pending Memory 清理、recall、router、agent 和 extraction enqueue，不包含 Memory compiled subgraph。
- fast/planning：每个顶层 run 仅 recall 一次，planning workers 共享同一快照。
- 调度契约：SDK 收到同一 thread、`assistant-memory-v1`、完整 messages、1800 秒和 enqueue。
- 隔离性：schedule 不直接调用 backend.commit；memory graph 不执行 Agent。
- debounce：连续 chat run 只 rollback 带专用 metadata 的旧 pending Memory run，不取消 pending chat run；使用 Agent Server 集成测试或真实 Studio trace 验证。
- 默认 pytest 保持 mock/offline；真实 Provider 只在明确真实验证时调用。

## 非目标

- 不实现自定义 Redis timer、`asyncio.sleep`、后台线程或 session manager。
- 不把 recall 放进 `create_agent.before_agent`。
- 不修改视觉模块。
- 不改变 fast/planning 的 Tool、HITL 或 stream 协议。
