# Task 017 真实 Provider 接入准备

## Goal

为真实 Provider 接入做准备，但不直接接入外部服务。

## Read first

- `docs/13-real-provider-integration.md`
- 当前 adapters 目录
- 当前 tools 目录
- 当前配置文件

## Scope

建立 Provider 配置和 Integration Test 隔离机制。

## Requirements

- 增加 ProviderConfig 或 settings。
- 支持通过环境变量读取 Provider 配置。
- 单元测试默认使用 MockAdapter。
- 集成测试需要 `RUN_INTEGRATION_TESTS=1` 才运行。
- 不写入任何 API Key。
- 不联网安装依赖。

## Suggested files

```text
src/multimodal_agent/config.py
tests/integration/
tests/integration/conftest.py
```

## Tests

- 无环境变量时不会失败。
- integration tests 默认 skip。
- 设置 `RUN_INTEGRATION_TESTS=1` 后才会尝试运行。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 018。
