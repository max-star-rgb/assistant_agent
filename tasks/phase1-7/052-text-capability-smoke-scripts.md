# Task 052 Text Capability Smoke Scripts

## Goal

为 direct_chat 和 text-only image_generation 提供手动 smoke 脚本。

## Read first

- `docs/52-text-capability-smoke-and-safety.md`
- 当前 scripts/
- 当前 config
- 当前 adapters

## Requirements

新增或更新：

```text
scripts/smoke_direct_chat.py
scripts/smoke_text_image_generation.py
```

要求：

- import 脚本不触发 Provider。
- 默认 mock smoke 可运行。
- 真实 Provider 只能用户显式设置环境变量后触发。
- 缺 key 时清晰提示。
- 不输出 API Key。
- 不提交真实生成图片。
- 真实生成目录写入 `.local/generated/` 或等价 ignored 路径。

## Tests

新增或更新：

```text
tests/test_text_capability_smoke_scripts.py
```

覆盖：

- import safe。
- missing key message。
- default mock path。
- output JSON structure。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 053。
