# Task 053 Text Capability Evals and API Coverage

## Goal

增强 direct_chat 和 image_generation 的 eval 与 API 覆盖。

## Read first

- 当前 `tests/evals/eval_cases.json`
- 当前 `scripts/run_evals.py`
- 当前 API routes
- `docs/53-phase5b-release-checklist.md`

## Requirements

- 增加 direct_chat eval cases。
- 增加 text-only image_generation eval cases。
- 确认 API 返回 direct_chat / image_generation 的 capability 和 tool results。
- 默认 eval 不调用真实 Provider。
- 如果 output contract 变化，更新测试。

## Tests

新增或更新：

```text
tests/test_text_capability_api.py
tests/test_text_capability_evals.py
```

## Acceptance

```bash
python scripts/run_evals.py
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 054。
