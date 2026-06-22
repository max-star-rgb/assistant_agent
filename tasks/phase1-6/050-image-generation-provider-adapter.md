# Task 050 Image Generation Provider Adapter

## Goal

为 image_generation 增加更明确的 adapter contract，并确保纯文本文生图路径稳定。

## Read first

- `docs/50-image-generation-provider-design.md`
- 当前 image generation tool / adapter
- 当前 routing and planner
- 当前 output schema

## Requirements

- 检查或定义 ImageGenerationAdapter Protocol。
- 检查或定义 ImageGenerationRequest / ImageGenerationResult。
- 确保纯文本 image_generation 不要求 image/video。
- 默认使用 MockImageGenerationAdapter。
- 可预留真实 Provider skeleton，但不默认调用。
- 真实生成输出目录必须被 `.gitignore` 忽略。
- 不提交真实生成图片。

## Tests

新增或更新：

```text
tests/test_image_generation_adapter.py
tests/test_text_only_image_generation.py
```

覆盖：

- text-only image_generation。
- mock output_ref。
- provider_unconfigured 或 mock default。
- 不触发 image_understanding。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 051。
