# 96 Provider Safety Eval and API Plan

## 目标

为 Provider Safety 建立测试、eval、API 覆盖，确保安全策略不是只停留在文档中。

## 测试分类

### Unit Tests

覆盖：

```text
ProviderError mapping
redaction
retry policy
budget policy
timeout config
fallback policy
```

### Integration-like Tests

默认使用 mock provider 模拟：

```text
timeout
rate limit
bad response
auth failed
unconfigured
budget exceeded
```

不调用真实 Provider。

### API Tests

覆盖：

```text
GET /runs/{run_id}
GET /traces/{trace_id}
error response redaction
provider safety error contract
```

### Eval Cases

增加 provider safety cases：

```text
provider_timeout_partial_result
provider_unconfigured_followup_or_error
budget_exceeded
bad_response
rate_limited
```

## run_evals 扩展

可以新增 suite：

```text
provider_safety
```

示例：

```bash
python scripts/run_evals.py --suite provider_safety
```

默认仍离线。

## Smoke

Phase 5H 不要求新增真实 smoke。

如果已有 smoke：

```text
smoke_real_vision.py
smoke_direct_chat.py
smoke_product_search.py
smoke_render_3d.py
smoke_video_understanding.py
```

应确保它们：

- 缺配置时清晰退出。
- 不输出 key。
- 不默认批量调用真实 Provider。
- 可显示 trace_id。

## 验收标准

- provider safety unit tests 存在。
- API trace query tests 存在。
- eval 有 provider_safety suite。
- 默认 pytest 不调用真实 Provider。
- 默认 eval 不调用真实 Provider。
- 敏感信息脱敏测试通过。
