# Task 008：图片生成适配器

## Goal

实现图片生成工具的统一接口和 mock 输出，支持“生成海报图、场景图、风格图”。

## Read first

- `docs/05-tool-contracts.md`
- `docs/01-architecture.md`

## Scope

新增/修改：

```text
src/multimodal_agent/services/image_generation_adapter.py
src/multimodal_agent/tools/image_generation_tool.py
tests/unit/test_image_generation.py
```

## Steps

1. 定义 `ImageGenerationAdapter`。
2. 实现 prompt 生成函数。
3. 实现 mock adapter，返回 generation_id、image_url、prompt。
4. 支持从商品信息 + 用户风格要求生成 prompt。

## Acceptance

```bash
pytest tests/unit/test_image_generation.py
```

必须验证：

- 输入“日系海报”生成包含商品与风格的 prompt。
- 返回结构化 ImageGenerationResult。

## Out of scope

- 不接真实文生图/图生图模型。
- 不下载或生成真实图片文件。
