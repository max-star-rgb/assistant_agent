# Task 035 Graph Execution Trace

## Goal

为 AgentGraphRuntime 增加 graph execution trace。

## Read first

- `docs/32-graph-execution-trace.md`
- 当前 run_history
- 当前 tool_history
- 当前 graph_nodes
- 当前 EventSink

## Requirements

- 定义 TraceEvent。
- 定义 TraceStore。
- 至少实现 InMemoryTraceStore。
- 可选实现 JsonlTraceStore。
- 每个 graph node 记录 started/finished。
- 工具失败记录 error。
- 不记录 API Key 或大文件内容。

## Tests

新增：

```text
tests/test_graph_execution_trace.py
```

覆盖：

- run_id / trace_id 存在。
- 节点路径可查询。
- 工具失败 trace。
- trace 不包含敏感字段。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 036。
