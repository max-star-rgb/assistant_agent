# Task 024 增加 Provider Adapter 契约测试

## Goal

为真实 Provider 接入建立契约测试，但不真正接入外部 Provider。

## Read first

- `docs/21-provider-contract-testing.md`
- 当前 services/*_adapter.py
- 当前 tools/*
- 当前 config.py

## Scope

新增 contract tests 和 integration test skip 机制。

## Requirements

- Contract tests 默认运行，只使用 MockAdapter。
- Integration tests 默认 skip。
- `RUN_INTEGRATION_TESTS=1` 时才启用 integration tests。
- 无 API Key 不失败，只 skip。
- 不联网安装依赖。
- 不写真正 API Key。

## Suggested files

```text
tests/contracts/
tests/contracts/test_vision_adapter_contract.py
tests/contracts/test_product_adapter_contract.py
tests/contracts/test_image_generation_adapter_contract.py
tests/contracts/test_render_adapter_contract.py
tests/integration/conftest.py
tests/integration/test_provider_config.py
```

## Acceptance

```bash
python -m pytest
```

并确认 integration tests 默认 skip。

## Stop condition

完成后停止，不要继续 Task 025。
