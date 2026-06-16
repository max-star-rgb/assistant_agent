# Task 092 Provider Safety Eval and API Coverage

## Goal

为 Provider Safety 增加 eval suite 和 API 测试覆盖。

## Read first

- `docs/96-provider-safety-eval-api-plan.md`
- 当前 `scripts/run_evals.py`
- 当前 `tests/evals/eval_cases.json`
- 当前 API tests
- 当前 provider safety tests

## Requirements

- 增加 provider_safety eval suite 或等价 category。
- 增加 timeout / bad_response / unconfigured / budget_exceeded / rate_limited mock cases。
- Eval 默认离线。
- API error response 使用统一 ProviderError / ApiError。
- Smoke 脚本缺配置时清晰退出且不泄露密钥。
- 不调用真实 Provider。

## Tests

新增或更新：

```text
tests/test_provider_safety_evals.py
tests/test_provider_safety_api_errors.py
tests/test_smoke_provider_safety.py
```

## Suggested commands

```bash
python scripts/run_evals.py --suite provider_safety
```

如果当前 eval runner 不适合新增 suite，可用兼容字段实现。

## Acceptance

```bash
python scripts/run_evals.py
python scripts/run_evals.py --suite provider_safety
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 093。
