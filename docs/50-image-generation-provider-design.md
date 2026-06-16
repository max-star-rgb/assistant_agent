# 50 Image Generation Provider Design

## 目标

让 image_generation 支持纯文本生成请求，并为真实图片生成 Provider 做好可选接入结构。

## 能力定义

image_generation 用于：

```text
文生图
海报生成
商品图生成
封面图生成
风格图生成
结合图片上下文的后续生成
```

Phase 5B 首先保证纯文本输入：

```text
生成一张赛博朋克风格海报
帮我生成一张日系极简商品图
做一张小红书封面
```

## 推荐链路

```text
AgentGraphRuntime
  ↓
image_generation capability
  ↓
ImageGenerationTool
  ↓
ImageGenerationAdapter
  ↓
MockImageGenerationAdapter / RealImageProviderAdapter
```

## ImageGenerationRequest

建议字段：

```text
prompt
negative_prompt optional
style optional
width optional
height optional
reference_image_refs optional
memory_context optional
user_id
session_id
```

## ImageGenerationResult

建议字段：

```text
image_url or image_path
provider
model
prompt_used
output_ref
status
errors
latency_ms optional
cost_estimate optional
```

## 默认实现

默认使用 MockAdapter，返回 local/mock URL 或 output_ref。

默认测试不调用真实图片生成 Provider。

## 真实 Provider 可选方向

后续可选：

```text
OpenAI Image
ComfyUI
Stable Diffusion WebUI
Flux service
自研 HTTP image generation service
```

## 输出安全

禁止默认提交真实生成图片。

建议真实生成结果写入本地 ignored 目录：

```text
.local/generated/
```

并确保 `.gitignore` 覆盖。

## 验收标准

- 纯文本 image_generation 不要求图片。
- Prompt mapping 稳定。
- 缺 Provider 配置时返回 provider_unconfigured 或使用 mock 默认。
- 默认 pytest 离线。
