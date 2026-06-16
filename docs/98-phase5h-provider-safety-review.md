# 98 Phase 5H Provider Safety Review

## 结论

Phase 5H Provider Safety / Retry / Cost / Trace Query 已完成 baseline。

本阶段没有新增真实 Provider，没有默认调用真实外部服务，没有写入 API Key，没有实现 MCP / Skills，也没有进入 Memory Hardening。默认运行、默认 pytest、默认 eval 和默认 smoke 安全路径继续使用 MockAdapter / LocalJsonAdapter 或本地策略验证。

## 1. ProviderError Taxonomy 状态

已统一 Provider 错误结构和错误码：

```text
src/multimodal_agent/services/provider_errors.py
```

核心对象：

```text
ProviderError
ProviderAdapterError
normalize_provider_error_code()
map_exception_to_provider_error()
build_provider_error()
```

已覆盖常见 Provider 错误：

```text
provider_unconfigured
provider_timeout
provider_bad_response
provider_auth_failed
provider_rate_limited
provider_network_error
provider_unavailable
provider_budget_exceeded
provider_call_limit_exceeded
provider_input_size_exceeded
```

API 层通过 `src/multimodal_agent/schemas/api.py` 将 provider error 映射为稳定外部 `ApiError`。

## 2. SafetyPolicy 状态

已实现：

```text
ProviderSafetyPolicy
sanitize_error_message()
sanitize_error_detail()
```

脱敏规则覆盖：

```text
API Key
Authorization
Bearer token
cookie
secret
password
完整 base64
隐私绝对路径
provider raw response
traceback
```

接入点包括 API error、recovery policy、trace store、tool boundary 和主要 provider adapter 失败结果。

## 3. Retry / Fallback / Timeout 状态

已实现：

```text
src/multimodal_agent/services/provider_policy.py
```

核心对象：

```text
TimeoutPolicy
RetryPolicy
FallbackPolicy
ProviderExecutionPolicy
```

当前行为：

- `provider_timeout`、`provider_network_error`、`provider_rate_limited`、`provider_unavailable` 可按策略重试。
- `provider_unconfigured`、`provider_auth_failed`、请求无效、输入过大等错误不重试。
- mock fallback 默认关闭，必须显式配置 `MULTIMODAL_AGENT_ALLOW_MOCK_FALLBACK=1` 才允许策略层打开。
- `ToolExecutor` 已执行有限重试，并记录 retry count。
- partial result 继续由 response composer 汇总说明。

## 4. ProviderCallBudget 状态

已实现：

```text
src/multimodal_agent/services/provider_budget.py
```

核心对象：

```text
ProviderCallBudget
ProviderCallRecord
```

当前能力：

- 每个 `AgentState` 持有 per-run provider budget。
- 记录 provider call count、capability、provider、model、latency、status、estimated_cost。
- 支持 `max_provider_calls_per_run`。
- 支持 `max_calls_per_capability`。
- 支持 `max_estimated_cost_per_run`，未知成本允许为 `None`。
- 支持 `max_input_bytes_per_run`。
- 超限时在 tool execution 前阻断，并返回结构化错误和 failed capability contract。

预算错误码：

```text
provider_budget_exceeded
provider_call_limit_exceeded
provider_input_size_exceeded
```

## 5. Trace Query 状态

已实现只读 trace/run 查询：

```text
src/multimodal_agent/services/trace_query.py
src/multimodal_agent/api/routes_agent.py
```

API：

```text
GET /runs/{run_id}
GET /traces/{trace_id}
GET /runs/{run_id}/tool-calls
```

查询能力：

- 按 `run_id` 返回 run summary。
- 按 `trace_id` 返回 trace summary。
- 查询 tool-call summary。
- 输出 node path、tools、providers、error count、retry count、budget exceeded 标记。

## 6. Redaction 状态

`TraceEvent` 已扩展 provider safety 摘要字段：

```text
capability
provider
model
status
latency_ms
error_code
input_summary
output_summary
```

Trace store 写入时会调用 redaction：

```text
redact_trace_event()
trace_event_summary()
trace_debug_summary()
```

Trace / API / WebSocket / eval 输出不得包含 API Key、Authorization header、Bearer token、完整 base64、完整 provider raw response 或敏感绝对路径。现有测试覆盖这些边界。

## 7. Eval / API 覆盖

Provider safety 测试覆盖：

```text
tests/test_provider_error_taxonomy.py
tests/test_provider_safety_policy.py
tests/test_sensitive_redaction.py
tests/test_retry_policy.py
tests/test_fallback_policy.py
tests/test_provider_timeout_policy.py
tests/test_provider_call_budget.py
tests/test_provider_budget_in_tool_executor.py
tests/test_budget_exceeded_response.py
tests/test_trace_query_api.py
tests/test_trace_redaction.py
tests/test_run_summary_query.py
tests/test_provider_safety_evals.py
tests/test_provider_safety_api_errors.py
tests/test_smoke_provider_safety.py
```

Eval 已新增：

```text
provider_safety
```

覆盖 case：

```text
provider_timeout
provider_bad_response
provider_unconfigured
provider_budget_exceeded
provider_rate_limited
```

运行方式：

```bash
python scripts/run_evals.py --suite provider_safety
```

## 8. 默认离线安全边界

已确认：

- 默认 Provider 仍是 MockAdapter / LocalJsonAdapter。
- 默认 pytest 不调用真实 Provider。
- 默认 eval 不调用真实 Provider。
- smoke 缺配置时清晰退出，不泄露密钥。
- 真实 Provider 仍只允许用户显式配置并手动运行 smoke 或 env-gated integration tests。
- 未新增真实 Provider。
- 未写入 API Key。
- 未提交真实 Provider 输出样本。

## 9. Phase 5I 建议

Phase 5H 后建议进入 Phase 5I Memory Hardening。

建议边界：

- 聚焦 memory data model、store boundary、retrieval ranking、write policy、privacy / user isolation、memory eval / API coverage。
- 继续保持默认离线。
- 不把 memory hardening 扩展为独立长期记忆平台。
- 不在 Phase 5I 中实现 MCP / Skills。

## 审计结论

Phase 5H 的 Provider Safety / Retry / Cost / Trace Query baseline 已完成。系统已经具备真实 Provider 未来接入前所需的基础保护：稳定错误结构、统一脱敏、有限重试、默认关闭 mock fallback、调用预算、trace 查询和 provider safety eval/API 覆盖。
