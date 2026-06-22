# Task 119 Healthcheck / Trace / Observability

## Goal

完善本地运行的健康检查和调试入口。

## Requirements

- 确认 `GET /health`。
- 确认 run_id / trace_id 查询。
- 文档说明如何调试 tool_calls / errors。
- 不引入复杂监控系统。

## Acceptance

```bash
python -m pytest
```
