# Task 099 Memory Eval / API / Demo Coverage

## Goal

为 memory hardening 增加 eval、API、demo runner 覆盖。

## Read first

- `docs/104-memory-eval-api-demo-plan.md`
- 当前 eval cases
- 当前 run_demo_flows.py
- 当前 API routes
- 当前 memory tests

## Requirements

- 增加 memory eval suite 或 category。
- 增加 preference memory → image_generation case。
- 增加 product memory → render_3d case。
- 增加 task resume case。
- 增加 user isolation case。
- API 或 runtime-level test 覆盖 save/search/delete。
- Demo runner 增加 memory scenarios。
- 默认 local memory。
- 不调用外部 memory service。

## Suggested eval command

```bash
python scripts/run_evals.py --suite memory
```

如果当前 runner 不适合新增 suite，可用兼容 category 实现。

## Tests

新增或更新：

```text
tests/test_memory_evals.py
tests/test_memory_api_or_runtime.py
tests/test_memory_demo_runner.py
```

## Acceptance

```bash
python scripts/run_evals.py
python scripts/run_evals.py --suite memory
python scripts/run_demo_flows.py
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 100。
