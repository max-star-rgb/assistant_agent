# 71 Response Composer Quality

## 目标

让 Agent 的最终回答从“已完成请求处理”升级为基于 tool results 的可读总结。

## 不等于接真实 API

Response composer 质量提升不要求调用真实 Provider。

它只基于：

```text
AgentState
tool_calls
tool_results
capability contracts
errors
memory_context
```

生成用户可读回答。

默认实现应采用：

```text
template-based response composer
```

不默认调用 LLM。

## 问题示例

当前不理想输出：

```text
已完成请求处理。
```

期望输出：

```text
我识别到图片中是一双白色低帮运动鞋，并找到 3 个相似商品。
最低价为 329 元，推荐理由是价格最低且标题匹配度较高。
我还生成了一张日系极简风海报，结果为 mock://image/generated/poster.png。
```

## Response Composer 分层

### 1. Direct Chat

如果没有工具调用，返回 ChatAdapter 的 response_text。

### 2. 单工具任务

根据 capability contract 生成摘要。

示例：

```text
image_generation → 已根据你的描述生成图片，结果为 ...
product_search → 已找到 N 个商品，其中推荐 ...
render_3d → 已创建 3D 渲染预览，结果为 ...
```

### 3. 多工具任务

按执行顺序总结：

```text
先理解了图片 → 再搜索商品 → 再比价 → 最后生成海报
```

### 4. 部分失败任务

应明确说明：

```text
已完成商品搜索，但比价失败，原因是没有可用价格信息。
```

### 5. 追问任务

如果缺必要输入，不应说完成，而应追问：

```text
你想让我基于这张图做什么？解释内容、找相似商品，还是生成图片？
```

## 模板化策略

建议新增：

```text
src/multimodal_agent/agent/response_templates.py
```

或在现有 response composer 中集中实现。

模板输入：

```text
capability
status
data
errors
tool_sequence
```

当前实现采用：

```text
src/multimodal_agent/agent/response_templates.py
src/multimodal_agent/agent/response_composer.py
```

`response_templates.py` 只读取 `CapabilityOutputContract`、错误列表和记忆摘要，不调用 LLM，不访问真实 Provider。

`response_composer.py` 保留旧字段兼容，同时优先基于 `ToolResult.contract` 生成最终回答。

## 多步总结示例

### 搜索 + 比价

```text
我已根据你的条件找到 5 个商品，并完成比价。最低价为 299 元，来自 mock 平台。综合价格和匹配度，推荐第一项。
```

### 图片 + 搜索 + 比价 + 生成

```text
我先识别了图片中的商品，然后搜索了相似款并完成比价。当前最低价为 329 元。同时我已生成一张商品海报，结果为 mock://image/generated/poster.png。
```

### 搜索 + 渲染

```text
我已找到符合条件的商品，并基于该商品生成了 3D 场景预览，预览结果为 mock://render/preview.png。
```

## 禁止事项

- 不默认调用真实 LLM。
- 不输出 provider raw response。
- 不输出 API Key / Authorization。
- 不编造真实价格、真实平台或真实链接。
- 不把 mock 结果伪装成真实外部结果。

## 验收标准

- 单工具结果有具体摘要。
- 多工具结果能按步骤总结。
- 部分失败有明确说明。
- 默认离线。
- 不调用真实 Provider。
