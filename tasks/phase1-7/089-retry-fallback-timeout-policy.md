# Task 089 Retry / Fallback / Timeout Policy

## Goal

实现统一 timeout、retry、fallback 策略，并确保 mock fallback 默认关闭。

## Read first

- `docs/93-retry-fallback-timeout-policy.md`
- 当前 provider adapters
- 当前 recovery policy
- 当前 runtime / tool executor

## Requirements

- 定义 TimeoutPolicy / RetryPolicy / FallbackPolicy 或等价结构。
- provider_unconfigured 不重试。
- provider_auth_failed 不重试。
- provider_timeout 可按策略重试。
- provider_rate_limited 可按策略重试。
- mock fallback 默认关闭。
- partial result 可被 response composer 说明。
- 不调用真实 Provider。

## Tests

新增或更新：

```text
tests/test_retry_policy.py
tests/test_fallback_policy.py
tests/test_provider_timeout_policy.py
tests/test_partial_result_response.py
```

覆盖：

- timeout retry。
- unconfigured no retry。
- auth failed no retry。
- mock fallback disabled。
- partial result summary。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 090。
