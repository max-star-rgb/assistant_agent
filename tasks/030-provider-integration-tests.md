# Task 030 Provider Integration Tests 完善

## Goal

完善真实 Provider 的 env-gated integration tests，确保默认测试离线。

## Read first

- `docs/27-real-provider-adapters.md`
- 当前 `tests/integration/`
- 当前 `config.py`
- Task 029 新增的真实 Provider Adapter

## Requirements

- Integration tests 默认 skip。
- 只有设置 `RUN_INTEGRATION_TESTS=1` 才执行。
- 缺少 Provider 配置时 skip，不失败。
- 不写入 API Key。
- 不要求 CI 默认调用真实服务。

## Suggested files

```text
tests/integration/test_real_provider_adapters.py
tests/integration/conftest.py
```

## Acceptance

```bash
python -m pytest
RUN_INTEGRATION_TESTS=1 python -m pytest tests/integration
```

第二条在缺配置时应 skip 或给出清晰提示，不应失败。

## Stop condition

完成后停止，不要继续 Task 031。
