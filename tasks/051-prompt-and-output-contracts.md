# Task 051 Prompt and Output Contracts

## Goal

统一 direct_chat 和 image_generation 的 prompt 构造与输出协议。

## Read first

- `docs/51-prompt-and-output-contracts.md`
- 当前 response composer
- 当前 tool input builder
- 当前 schemas

## Requirements

- 新增 PromptBuilder 或等价函数。
- direct_chat prompt 可注入 memory_context。
- image_generation prompt 可注入 style、product_context、visual_summary、memory_context。
- 控制 prompt 长度。
- 输出协议不泄露 provider raw response。
- 错误结构统一。

## Tests

新增或更新：

```text
tests/test_prompt_builder.py
tests/test_text_capability_output_contracts.py
```

覆盖：

- direct_chat prompt。
- image_generation prompt。
- memory_context 注入。
- prompt 长度限制。
- output contract 稳定。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 052。
