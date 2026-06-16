# 70 Capability Output Contract Unification

## 目标

统一各 capability 的输出结构，让 response composer、API、WebSocket、eval 和 demo runner 都能稳定读取结果。

## 当前问题

不同 capability 的结果可能来自不同 Tool / Adapter：

```text
direct_chat
image_generation
image_understanding
video_understanding
product_search
price_compare
render_3d
memory_retrieval
```

如果每个结果结构不统一，response composer 需要到处写特例，E2E demo 也难以稳定验证。

## 统一 Contract

建议所有 capability 输出统一包装为：

```json
{
  "capability": "product_search",
  "status": "succeeded",
  "output_ref": "mock://product/search/...",
  "data": {},
  "errors": [],
  "metadata": {}
}
```

当前实现入口：

```text
src/multimodal_agent/schemas/capability_output.py
```

核心 schema：

```text
CapabilityOutputContract
CapabilityOutputError
```

`ToolResult` 保留原有字段，并新增可选字段：

```text
contract: CapabilityOutputContract | None
```

## 字段定义

### capability

能力名：

```text
direct_chat
image_generation
image_understanding
video_understanding
product_search
price_compare
render_3d
memory_retrieval
```

### status

```text
succeeded
failed
partial
skipped
```

### output_ref

用于引用结果：

```text
mock://...
local://...
provider://...
memory://...
```

### data

结构化结果，禁止直接塞 provider raw response。

### errors

统一错误结构：

```json
{
  "code": "provider_unconfigured",
  "message": "Provider is not configured.",
  "detail": {},
  "recoverable": true
}
```

### metadata

允许包含：

```text
provider
model
latency_ms
source
```

禁止包含：

```text
API Key
Authorization header
Bearer token
完整 base64 图片
provider raw response
```

## 各能力 data 建议

### direct_chat

```json
{
  "response_text": "...",
  "prompt_used": "optional"
}
```

### image_generation

```json
{
  "image_url": "local://...",
  "prompt_used": "...",
  "style": "..."
}
```

### image_understanding / video_understanding

```json
{
  "objects": [],
  "scene": "",
  "summary": "",
  "style_tags": []
}
```

### product_search

```json
{
  "items": [],
  "query_used": "",
  "total": 0
}
```

### price_compare

```json
{
  "offers": [],
  "best_offer": {},
  "ranking_reason": ""
}
```

### render_3d

```json
{
  "preview_url": "mock://...",
  "model_url": "mock://...",
  "scene_description": ""
}
```

### memory_retrieval

```json
{
  "items": [],
  "memory_context": ""
}
```

## 兼容策略

不要一次性删除旧字段。可以：

1. 保留旧 ToolResult。
2. 新增 `contract` 字段。
3. response composer 优先读取 `contract`。
4. 逐步迁移 eval/API 到统一 contract。

当前兼容策略：

- `ToolResult.data` 保持原样。
- API `tool_results` 会同时返回旧字段和 `contract`。
- 部分旧调用仍可从 `data["contract"]` 读取兼容 contract。
- WebSocket `tool_finished` / `tool_failed` 事件只放 contract summary，不放完整 provider payload。

## 验收标准

- 所有核心 capability 至少能输出 contract。
- API 可返回 contract。
- WebSocket 可返回 contract 摘要。
- response composer 可基于 contract 生成自然回复。
- 默认测试离线。
