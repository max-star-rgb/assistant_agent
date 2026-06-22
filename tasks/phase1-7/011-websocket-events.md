# Task 011：WebSocket 事件与长任务进度

## Goal

为图片生成、3D 渲染等长任务预留实时进度推送接口。

## Read first

- `docs/07-service-api.md`
- `docs/09-security-observability-cost.md`

## Scope

新增/修改：

```text
src/multimodal_agent/api/websocket.py
src/multimodal_agent/schemas/events.py
tests/integration/test_websocket_events.py
```

## Steps

1. 定义 WebSocket event schema。
2. 实现 `/ws/agent/{session_id}`。
3. 支持发送 tool_started、tool_progress、tool_completed、agent_response。
4. 使用 mock 事件测试连接和消息格式。

## Acceptance

```bash
pytest tests/integration/test_websocket_events.py
```

必须通过。

## Out of scope

- 不实现真实渲染进度。
- 不接 Redis pub/sub。
