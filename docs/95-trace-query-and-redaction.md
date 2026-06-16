# 95 Trace Query and Redaction

## 目标

增强 Trace 查询能力，让开发者能根据 run_id / trace_id 调试 Agent 执行路径，同时保证敏感信息不泄露。

## 当前 Trace 能力

系统已有：

```text
run_id
trace_id
graph node events
tool events
errors
```

Phase 5H 要补强：

```text
trace query API
redaction policy
provider safety trace fields
debug summary
```

## Trace 查询 API

建议新增只读接口：

```text
GET /runs/{run_id}
GET /traces/{trace_id}
GET /runs/{run_id}/tool-calls
```

或等价 API。

## TraceEvent 建议字段

```text
trace_id
run_id
node_name
event_type
capability
tool_name
provider
model
status
latency_ms
error_code
input_summary
output_summary
created_at
```

## 禁止记录

Trace 不得记录：

```text
API Key
Authorization header
Bearer token
cookie
secret
password
完整 base64
完整图片/视频内容
完整 provider raw response
隐私绝对路径
```

## RedactionPolicy

建议新增：

```text
redact_secret(value)
redact_headers(headers)
redact_large_payload(payload)
redact_file_path(path)
```

## input_summary / output_summary

允许记录：

```text
input type
file size
media count
prompt length
result item count
error code
provider name
model name
```

不允许记录：

```text
完整 Prompt 中的敏感字段
完整媒体内容
原始 Provider 响应
```

## Debug Summary

可以为每次 run 输出：

```text
执行了哪些节点
调用了哪些工具
哪些 Provider 被调用
是否发生错误
是否触发 retry
是否 budget exceeded
```

## 验收标准

- 可通过 run_id 查询 run summary。
- 可通过 trace_id 查询 trace summary。
- Trace 中敏感字段被脱敏。
- Provider error 可通过 trace 定位。
- 默认 API 不泄露 raw provider response。
- 默认测试离线。
