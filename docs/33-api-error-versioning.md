# 33 API 错误码与响应协议版本设计

## 目标

稳定前后端边界，避免前端依赖内部 AgentState 结构。

## API 响应原则

对外响应应稳定，内部状态可演进。

推荐响应：

```json
{
  "protocol_version": "v1",
  "run_id": "...",
  "status": "success",
  "response": "...",
  "data": {},
  "errors": [],
  "trace_id": "..."
}
```

## 错误结构

```python
class ApiError(BaseModel):
    code: str
    message: str
    detail: dict = {}
    recoverable: bool = False
```

## 错误码建议

```text
INVALID_REQUEST
INTENT_UNCLEAR
TOOL_NOT_FOUND
TOOL_INPUT_INVALID
PROVIDER_UNCONFIGURED
PROVIDER_TIMEOUT
PROVIDER_RATE_LIMITED
MEMORY_UNAVAILABLE
TASK_NOT_FOUND
TASK_FAILED
INTERNAL_ERROR
```

## WebSocket 错误事件

```json
{
  "event_type": "task_failed",
  "run_id": "...",
  "error": {
    "code": "PROVIDER_TIMEOUT",
    "message": "图像生成服务超时",
    "recoverable": true
  }
}
```

## 验收标准

- HTTP API 返回 `protocol_version`。
- 错误响应结构统一。
- WebSocket 错误事件结构统一。
- 测试覆盖常见错误码。
