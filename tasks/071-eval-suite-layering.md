# Task 071 Eval Suite Layering

## Goal

将 eval suite 按 routing、tool contract、API contract、E2E demo 分层，降低后续维护成本。

## Read first

- `docs/72-eval-suite-layering.md`
- 当前 `tests/evals/eval_cases.json`
- 当前 `scripts/run_evals.py`
- 当前 tests/

## Requirements

- Eval case 支持 suite/category 字段。
- `scripts/run_evals.py` 支持按 suite 运行，或至少输出 suite-level summary。
- 默认仍运行离线 eval。
- 不调用真实 Provider。
- 不破坏现有 eval case。
- E2E demo eval 可引用 demo scenario matrix。

## Suggested commands

```bash
python scripts/run_evals.py
python scripts/run_evals.py --suite routing
python scripts/run_evals.py --suite e2e
```

如果当前 CLI 不适合增加参数，可用兼容方式实现。

## Tests

新增或更新：

```text
tests/test_eval_suite_layering.py
```

覆盖：

- suite 字段。
- suite-level summary。
- 默认离线。
- failed_case_ids 保留。

## Acceptance

```bash
python scripts/run_evals.py
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 072。
