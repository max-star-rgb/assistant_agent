# 28 长任务事件流与 WebSocket 设计

## 背景

当前 WebSocket 已通过 Graph Runtime 输出事件，但仍偏同步、本地、短任务。

Phase 4 目标是把它升级为长任务事件流，为图片生成、3D 渲染、视频理解等耗时任务做准备。

## 事件模型

推荐事件类型：

```text
task_started
graph_node_started
graph_node_finished
tool_started
tool_progress
tool_finished
tool_failed
memory_loaded
memory_saved
response_delta
final_response
task_failed
```

## Event Schema

推荐新增或扩展：

```python
class AgentEvent(BaseModel):
    event_id: str
    session_id: str
    run_id: str
    event_type: str
    node_name: str | None = None
    tool_name: str | None = None
    payload: dict = {}
    error: dict | None = None
    created_at: datetime
```

## 同步与异步边界

Phase 4 先做“本地异步抽象”，不强制引入 Redis/Celery。

推荐：

```text
AgentGraphRuntime
  ↓
TaskRunner
  ↓
EventSink
  ↓
WebSocket
```

## EventSink

定义统一事件出口：

```python
class EventSink(Protocol):
    def emit(self, event: AgentEvent) -> None:
        ...
```

实现：

```text
InMemoryEventSink
ListEventSink
WebSocketEventSink
```

## WebSocket 行为

WebSocket 不应该伪造固定事件，而应订阅 runtime 执行产生的事件。

## 验收标准

- WebSocket 返回的事件来自 runtime event sink。
- 测试可验证事件顺序。
- 工具失败时产生 `tool_failed` 和 `task_failed` 或降级事件。
- 不依赖外部队列。
