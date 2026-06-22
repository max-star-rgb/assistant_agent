# Task 034 失败恢复策略

## Goal

建立统一 Failure Recovery Policy，让多步 Agent 在工具失败时能结构化处理。

## Read first

- `docs/31-failure-recovery-policy.md`
- 当前 AgentState errors
- 当前 tool_executor
- 当前 response_composer
- 当前 graph loop

## Requirements

- 新增或扩展 RecoveryPolicy。
- Tool 失败转为结构化 error。
- 可选步骤失败允许继续。
- 关键步骤失败返回结构化错误。
- final response 说明部分成功/失败。
- 不吞掉异常信息，但不暴露敏感信息。

## Tests

新增：

```text
tests/test_failure_recovery_policy.py
```

覆盖：

- provider_unconfigured。
- tool timeout 模拟。
- optional step failed but continue。
- required step failed then stop。
- final response 包含失败说明。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 035。
