# 05 工具接口契约

## 1. 统一工具接口

所有工具都实现同一抽象接口：

```python
class BaseTool(Protocol):
    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]

    def run(self, input: BaseModel, context: ToolContext) -> ToolResult:
        ...
```

## 2. ToolResult

```python
class ToolResult(BaseModel):
    tool_name: str
    success: bool
    data: dict | None = None
    error: str | None = None
    output_ref: str | None = None
    latency_ms: int | None = None
```

## 3. 工具清单

### 3.1 VisionUnderstandingTool

输入：图片/视频 ID、文本问题。
输出：对象、颜色、材质、场景、风格、OCR、摘要。

### 3.2 ProductSearchTool

输入：视觉摘要、文本条件、预算、品牌、平台。
输出：商品列表、相似度、价格、购买链接、推荐理由。

### 3.3 PriceCompareTool

输入：商品候选列表。
输出：按价格/评分/平台可信度排序后的结果。

### 3.4 ImageGenerationTool

输入：商品信息、视觉摘要、用户风格要求、参考图。
输出：生成任务 ID、图片 URL、prompt、状态。

### 3.5 Render3DTool

输入：商品图片/模型、目标场景、材质、光照、镜头。
输出：渲染任务 ID、预览 URL、模型 URL、状态。

### 3.6 MemoryTool

输入：保存或检索请求。
输出：记忆项、命中原因、相关度。

## 4. 错误处理

工具失败时必须返回结构化错误：

```python
ToolResult(
    tool_name="product_search",
    success=False,
    error="缺少商品描述，无法搜索",
)
```

Agent 根据错误决定：重试、追问、降级、终止。

## 5. Mock 优先

所有工具先实现 mock：

- 不访问真实网络。
- 输出稳定可测试。
- 保留真实 adapter 的接口位置。

真实服务接入放在后续独立任务中。
