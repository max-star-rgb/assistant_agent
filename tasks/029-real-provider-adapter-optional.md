# Task 029 真实 Provider Adapter 可选实现

## Goal

新增至少一个真实 Provider Adapter 的可选实现，但默认仍使用 MockAdapter。

## Read first

- `docs/27-real-provider-adapters.md`
- `src/multimodal_agent/config.py`
- 当前 `src/multimodal_agent/services/*_adapter.py`
- 当前 Tool Registry

## Scope

优先选择一个最容易实现和隔离的 Provider。

推荐顺序：

1. Vision Provider
2. Image Generation Provider
3. Product Search Provider
4. Render Provider

## Requirements

- Tool 层不直接调用 Provider。
- Provider 只存在于 Adapter 实现中。
- 默认 provider 必须仍为 `mock`。
- 无环境变量时不失败。
- 不写 API Key。
- 不自动联网安装依赖。
- Provider 错误转为结构化错误。

## Tests

新增或更新：

```text
tests/test_provider_selection.py
tests/contracts/
```

至少覆盖：

- 默认 mock。
- 配置真实 provider 但缺 key 时给出 provider_unconfigured。
- Tool 层不需要修改即可切换 Adapter。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 030。
