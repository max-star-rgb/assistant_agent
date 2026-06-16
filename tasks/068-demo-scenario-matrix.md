# Task 068 Demo Scenario Matrix

## Goal

定义一组默认离线可运行的 E2E demo scenarios。

## Read first

- `docs/69-demo-scenario-matrix.md`
- 当前 eval cases
- 当前 demo_data/
- 当前 README

## Requirements

新增或更新：

```text
demo_data/scenarios/e2e_demo_scenarios.json
docs/69-demo-scenario-matrix.md
```

至少包含 8 个场景：

```text
text_chat
text_image_generation
image_understanding
video_understanding
product_search_compare
image_to_product_search_compare
product_search_to_image_generation
product_search_to_render
```

建议额外包含：

```text
image_to_render
memory_to_image_generation
full_multistep_image_search_compare_generate
ambiguous_followup
```

每个 scenario 包含：

```text
scenario_id
title
user_query
metadata
expected_tools
expected_response_contains
```

## Requirements

- 默认离线。
- 不依赖真实图片/视频。
- 媒体场景可通过 metadata 模拟。
- 不提交真实媒体文件。
- 不调用真实 Provider。

## Tests

新增或更新：

```text
tests/test_demo_scenario_matrix.py
```

覆盖：

- scenario 文件可解析。
- scenario_id 唯一。
- expected_tools 字段存在。
- 不包含真实绝对路径或 API Key。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 069。
