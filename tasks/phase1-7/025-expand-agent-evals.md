# Task 025 扩展 Agent Eval 集

## Goal

把 eval cases 从基础样例扩展到至少 30 条，并输出更细分指标。

## Read first

- `docs/22-evaluation-expansion.md`
- `tests/evals/eval_cases.json`
- `scripts/run_evals.py`

## Scope

扩展 eval cases 和 runner。

## Requirements

至少包含：

- 5 条 intent-only。
- 5 条 tool routing。
- 8 条 multi-step。
- 4 条 memory。
- 4 条 failure / ambiguous input。
- 4 条 multimodal input combination。

Runner 输出：

```text
total
passed
failed
pass_rate
intent_accuracy
tool_selection_accuracy
ordered_tool_match
unexpected_tool_rate
failed_case_ids
```

## Tests

- eval case 文件可解析。
- runner 可以离线运行。
- 不依赖真实 Provider。

## Acceptance

```bash
python scripts/run_evals.py
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 026。
