# 51 Prompt and Output Contracts

## 目标

统一 direct_chat 和 image_generation 的输入、输出和 prompt 构造，避免能力实现散落在 route / tool / adapter 中。

## PromptBuilder

建议新增：

```text
src/multimodal_agent/agent/prompt_builder.py
```

职责：

- 根据 intent 构造 direct_chat prompt。
- 根据 image_generation 请求构造 generation prompt。
- 注入 memory_context。
- 注入视觉摘要或商品摘要。
- 控制 prompt 长度。
- 不写 provider-specific payload。

## Direct Chat Prompt

输入：

```text
user_query
memory_context optional
conversation_context optional
system_instruction optional
```

输出：

```text
ChatRequest
```

## Image Generation Prompt

输入：

```text
user_query
style
product_context optional
visual_summary optional
memory_context optional
```

输出：

```text
ImageGenerationRequest
```

## Output Contract

对外响应不应泄露内部 Provider 原始 JSON。

推荐：

```json
{
  "capability": "image_generation",
  "status": "succeeded",
  "output_ref": "mock://image/...",
  "data": {
    "image_url": "...",
    "prompt_used": "..."
  },
  "errors": []
}
```

## 错误结构

统一使用：

```text
code
message
detail
recoverable
```

## 验收标准

- prompt 构造可单测。
- adapter 只接收结构化 request。
- response composer 不需要理解 provider raw response。
- API 输出结构稳定。
