# 30 Memory 检索策略增强设计

## 当前状态

Phase 3 已支持：

```text
MULTIMODAL_AGENT_MEMORY_BACKEND=memory|jsonl
```

Graph Runtime 可以加载和保存 memory。

但检索策略仍较基础。

## Phase 4 目标

增强本地记忆检索，不引入 Vector DB。

支持：

- 关键词检索。
- 类型过滤。
- 用户过滤。
- 会话过滤。
- 时间过滤。
- 摘要压缩。
- Top-K 返回。
- Memory Context 格式化。

## Memory 类型

```text
conversation
video
product
preference
task
generation
render
```

## MemoryQuery

建议扩展：

```python
class MemoryQuery(BaseModel):
    user_id: str
    session_id: str | None = None
    query: str
    memory_types: list[str] = []
    top_k: int = 5
    since: datetime | None = None
```

## MemoryContext

Graph 不应拿到原始海量 memory。

应拿到格式化后的 context：

```text
相关历史：
1. 上次用户关注白色低帮运动鞋。
2. 用户偏好日系、极简、浅色背景。
3. 最近生成过一张商品海报。
```

## 摘要压缩

Phase 4 先做规则压缩：

- 限制条数。
- 限制字符数。
- 按时间倒序。
- 按类型优先级。
- 去重。

不调用 LLM 总结。

## 验收标准

- 支持类型过滤。
- 支持 Top-K。
- 支持格式化 memory_context。
- 跨 runtime 实例可从 JSONL 检索。
