# Task 091 Trace Query and Redaction

## Goal

增强 trace 查询能力，并确保 trace/API 不泄露敏感信息。

## Read first

- `docs/95-trace-query-and-redaction.md`
- 当前 trace store
- 当前 run history
- 当前 API routes
- 当前 WebSocket events

## Requirements

- 增加只读 trace/run 查询 API 或等价服务函数。
- 支持按 run_id 查询 summary。
- 支持按 trace_id 查询 summary。
- TraceEvent 包含 provider safety 相关摘要字段。
- input_summary / output_summary 不包含敏感 payload。
- 应用 redaction policy。
- 不暴露 raw provider response。
- 默认测试离线。

## Suggested endpoints

如当前 API 结构允许：

```text
GET /runs/{run_id}
GET /traces/{trace_id}
GET /runs/{run_id}/tool-calls
```

如果不适合新增 HTTP endpoint，可先实现 service-level query 并测试。

## Tests

新增或更新：

```text
tests/test_trace_query_api.py
tests/test_trace_redaction.py
tests/test_run_summary_query.py
```

覆盖：

- query by run_id。
- query by trace_id。
- redacted secret。
- no base64。
- no raw provider response。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 092。
