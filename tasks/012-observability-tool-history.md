# Task 012：观测与工具调用历史

## Goal

记录每次 Agent Run 和 Tool Call，便于调试、成本统计和后续审计。

## Read first

- `docs/09-security-observability-cost.md`
- `docs/03-agent-state.md`

## Scope

新增/修改：

```text
src/multimodal_agent/services/run_history.py
src/multimodal_agent/services/tool_history.py
tests/unit/test_history.py
```

## Steps

1. 实现 run history 接口。
2. 实现 tool call history 接口。
3. MVP 可使用 JSONL 写入 `.data/`。
4. 在 workflow 中记录 run start/end 和 tool call start/end。
5. 测试日志写入和读取。

## Acceptance

```bash
pytest tests/unit/test_history.py
```

必须验证：

- 每个 run 有 run_id。
- 每次工具调用有 call_id、tool_name、status、latency。
- 失败工具调用也被记录。

## Out of scope

- 不接 OpenTelemetry。
- 不接真实成本计费系统。
