# Task 046 Routing Evals and Regression Suite

## Goal

扩展离线 eval cases，覆盖所有 Assistant capability routing。

## Read first

- `docs/44-assistant-routing-eval-plan.md`
- 当前 `tests/evals/eval_cases.json`
- 当前 `scripts/run_evals.py`

## Requirements

Eval 至少覆盖：

```text
direct_chat
text_only_image_generation
text_only_product_search
text_only_price_compare
text_only_render
image_understanding
video_understanding
media_plus_generation
media_plus_search_compare
memory_retrieval
multi_step_orchestration
ambiguous_followup
```

## Minimum cases

至少 40 条 routing eval cases。

## Metrics

如当前 runner 不支持，可以逐步增加：

```text
intent_accuracy
capability_accuracy
tool_selection_accuracy
ordered_tool_match
unexpected_tool_rate
media_requirement_error_rate
followup_accuracy
failed_case_ids
```

## Requirements

- 默认离线运行。
- 不调用真实 Provider。
- 不提交真实图片/视频。
- 用 metadata 模拟 has_image / has_video。

## Acceptance

```bash
python scripts/run_evals.py
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 047。
