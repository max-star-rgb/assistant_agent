# Task 031 长任务事件流与 WebSocket 升级

## Goal

让 WebSocket 事件来自 runtime event sink，而不是固定 mock progress。

## Read first

- `docs/28-async-task-and-websocket.md`
- 当前 `src/multimodal_agent/api/websocket.py`
- 当前 `AgentGraphRuntime`
- 当前 tool history / run history

## Scope

新增 AgentEvent 和 EventSink 抽象。

## Requirements

- 定义 `AgentEvent` schema。
- 定义 `EventSink` 协议或基础类。
- Runtime 执行时 emit 节点和工具事件。
- WebSocket 读取真实事件。
- 保留 mock helper 仅用于测试或 fallback。
- 不引入外部队列。

## Tests

新增或更新：

```text
tests/test_agent_events.py
tests/test_websocket_event_stream.py
```

覆盖：

- event 顺序。
- tool_started/tool_finished。
- final_response。
- tool_failed 或 task_failed。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 032。
