# Task 072 E2E Demo Runner

## Goal

新增默认离线的 E2E demo runner，用于演示 Assistant Agent 的完整能力链路。

## Read first

- `docs/73-e2e-demo-runner.md`
- `demo_data/scenarios/e2e_demo_scenarios.json`
- 当前 AgentGraphRuntime
- 当前 scripts/
- 当前 eval runner

## Requirements

新增：

```text
scripts/run_demo_flows.py
```

要求：

- 默认运行全部 demo scenarios。
- 支持 `--scenario` 运行指定 scenario。
- 输出 JSON summary。
- 每个 scenario 输出：
  - scenario_id
  - status
  - tool_sequence
  - response_text
  - errors
  - run_id
  - trace_id
- 默认 mock/local。
- 不调用真实 Provider。
- 不要求真实媒体文件。
- 不输出 API Key / raw provider response / base64。

## Tests

新增：

```text
tests/test_e2e_demo_runner.py
```

覆盖：

- runner import safe。
- run all mock scenarios。
- run single scenario。
- output JSON structure。
- tool sequence 可验证。
- response_text 不是通用“已完成请求处理”。

## Acceptance

```bash
python scripts/run_demo_flows.py
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 073。
