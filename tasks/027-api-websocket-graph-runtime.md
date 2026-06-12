# Task 027 API/WebSocket 使用 Graph Runtime

## Goal

让 HTTP API 和 WebSocket 事件流都围绕 Graph Runtime，而不是 mock progress 或旧 workflow。

## Read first

- `docs/18-langgraph-primary-runtime.md`
- 当前 `src/multimodal_agent/api/routes_agent.py`
- 当前 `src/multimodal_agent/api/websocket.py`
- 当前 AgentGraphRuntime

## Scope

更新 API 和 WebSocket runtime 入口。

## Requirements

- HTTP `/agent/run` 默认调用 AgentGraphRuntime。
- WebSocket 至少能基于 graph runtime 输出真实阶段事件。
- 保留 mock websocket helper 仅用于测试或 fallback。
- 不调用真实 Provider。
- 测试默认离线。

## Tests

新增或更新：

```text
tests/test_api_graph_runtime.py
tests/test_websocket_graph_runtime.py
```

覆盖：

- HTTP run。
- WebSocket event sequence。
- 工具调用历史可观察。
- 错误路径返回结构化错误。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 028。
