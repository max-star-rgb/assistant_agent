# Task 078 Planner Quality and Slot Filling

## Goal

提升 Task Planner 对多步任务、缺失输入和 slot filling 的处理质量。

## Read first

- `docs/80-planner-quality-and-slot-filling.md`
- 当前 planner
- 当前 tool_input_builder
- 当前 CapabilityValidator
- 当前 LangGraph loop

## Requirements

- PlanStep 支持 depends_on / input_refs / required_inputs / optional / reason。
- Planner 可补全 search → compare。
- Planner 可识别 memory → image_generation。
- Planner 可识别 image_understanding → product_search → price_compare → image_generation。
- Planner 可识别 product_search → render_3d。
- 缺关键输入时 ask_followup。
- 可选字段缺失时允许继续。
- 不调用真实 Provider。

## Tests

新增或更新：

```text
tests/test_planner_slot_filling.py
tests/test_planner_missing_inputs.py
tests/test_planner_multistep_quality.py
```

覆盖：

- query-only price_compare auto-search。
- missing image for image_understanding。
- missing video for video_understanding。
- missing render scene。
- memory reference + generation。
- image → search → compare → generation。
- product → render。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 079。
