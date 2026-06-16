# Task 116 Real Provider Smoke Matrix

## Goal

建立真实 Provider smoke matrix，明确哪些已支持、哪些暂缓。

## Requirements

新增：

```text
docs/real-provider-smoke-matrix.md
```

字段：

```text
provider
capability
status
required_env
smoke_script
default_enabled
notes
```

要求：

- default_enabled 必须是 false。
- 不调用真实 Provider。
- 不写 API Key。

## Acceptance

```bash
python -m pytest
```
