# Task 088 Provider Error Taxonomy and Safety Policy

## Goal

统一 Provider 错误码、错误结构和脱敏策略。

## Read first

- `docs/92-provider-error-taxonomy-and-safety-policy.md`
- 当前 provider adapters
- 当前 ApiError / error schemas
- 当前 recovery policy
- 当前 trace code

## Requirements

- 定义 ProviderError schema 或统一现有 error schema。
- 定义 ProviderSafetyPolicy 或等价安全工具。
- 实现 provider error mapping helper。
- 实现 sensitive data redaction helper。
- 统一常见错误码。
- Adapter 不直接返回 raw exception。
- API / Trace 不泄露敏感信息。
- 不调用真实 Provider。

## Tests

新增或更新：

```text
tests/test_provider_error_taxonomy.py
tests/test_provider_safety_policy.py
tests/test_sensitive_redaction.py
```

覆盖：

- provider_unconfigured。
- provider_timeout。
- provider_bad_response。
- provider_auth_failed。
- API Key / Bearer / Authorization 脱敏。
- raw traceback 不直接暴露。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 089。
