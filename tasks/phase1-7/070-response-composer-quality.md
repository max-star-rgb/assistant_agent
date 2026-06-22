# Task 070 Response Composer Quality

## Goal

改进 response composer，使最终回答能基于 tool_results / capability contracts 生成具体、可读的总结。

## Read first

- `docs/71-response-composer-quality.md`
- 当前 response_composer
- 当前 capability contracts
- 当前 tool results
- 当前 eval cases

## Requirements

- 默认使用 template-based response composer。
- 不默认调用真实 LLM。
- direct_chat 返回 chat response。
- 单工具任务生成具体摘要。
- 多工具任务按执行顺序总结。
- 部分失败任务说明成功和失败部分。
- ask_followup 返回明确追问。
- 不编造真实平台、真实价格、真实链接。
- 不把 mock 结果伪装成真实 Provider 结果。
- 不输出 provider raw response。

## Tests

新增或更新：

```text
tests/test_response_composer_quality.py
tests/test_multistep_response_summary.py
```

覆盖：

- direct_chat。
- image_generation。
- product_search + price_compare。
- image_understanding + product_search + price_compare + image_generation。
- product_search + render_3d。
- partial failure。
- ask_followup。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 071。
