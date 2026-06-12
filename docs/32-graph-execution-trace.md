# 32 Graph Execution Trace 设计

## 目标

为 LangGraph 执行过程增加可调试 Trace，记录每次 run 的节点路径、状态变化、工具调用和错误。

## Trace 与 History 的关系

当前已有：

```text
run_history
tool_history
```

Phase 4 增加更细粒度：

```text
graph_trace
```

## TraceEvent

推荐：

```python
class TraceEvent(BaseModel):
    trace_id: str
    run_id: str
    node_name: str
    event_type: str
    before_state_summary: dict = {}
    after_state_summary: dict = {}
    tool_name: str | None = None
    error: dict | None = None
    created_at: datetime
```

## 不记录什么

不要记录：

- API Key。
- 原始大文件内容。
- 完整图片/视频二进制。
- 超长模型输入。
- 用户隐私原文的无必要副本。

## Trace 输出

支持：

```text
InMemoryTraceStore
JsonlTraceStore
```

## 用途

- 调试 graph 分支。
- 检查多步顺序。
- Eval 失败时定位。
- WebSocket 事件来源。
- 发布审计。

## 验收标准

- 每次 AgentGraphRuntime.run 生成 run_id / trace_id。
- 每个 graph node 至少记录 started/finished。
- 工具失败记录 error。
- 测试可断言节点路径。
