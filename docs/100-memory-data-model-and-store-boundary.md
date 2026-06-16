# 100 Memory Data Model and Store Boundary

## 目标

统一 memory 数据模型和 store 边界，让 memory_retrieval / memory_save 不再只是 mock 工具，而是可被 planner、response composer、demo runner 可靠使用的本地能力。

## MemoryItem

建议定义或整理：

```python
class MemoryItem(BaseModel):
    memory_id: str
    user_id: str
    session_id: str | None = None
    memory_type: str
    summary: str
    content: dict = {}
    tags: list[str] = []
    source: str = "agent"
    artifact_refs: list[str] = []
    created_at: datetime
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    sensitivity: str = "normal"
```

## memory_type

建议支持：

```text
conversation
preference
product
artifact
task
image
video
render
```

## MemoryQuery

建议字段：

```python
class MemoryQuery(BaseModel):
    user_id: str
    session_id: str | None = None
    query: str
    memory_types: list[str] = []
    tags: list[str] = []
    top_k: int = 5
    since: datetime | None = None
    include_expired: bool = False
```

## MemorySearchResult

建议字段：

```text
items
query_used
total
ranking_reason
memory_context
errors
```

## MemoryStore Interface

建议统一：

```python
class MemoryStore(Protocol):
    def save(self, item: MemoryItem) -> MemoryItem:
        ...

    def search(self, query: MemoryQuery) -> MemorySearchResult:
        ...

    def get(self, memory_id: str, user_id: str) -> MemoryItem | None:
        ...

    def delete(self, memory_id: str, user_id: str) -> bool:
        ...
```

## 当前 Store 边界

默认支持：

```text
InMemoryStore
JsonlMemoryStore
```

可选预留：

```text
SqliteMemoryStore skeleton
VectorMemoryStore skeleton
```

但 Phase 5I 不要求接真实数据库或向量库。

## Artifact 引用

Memory 不应保存大文件本体，只保存引用：

```text
mock://image/...
local://generated/...
mock://render/...
provider://vision/...
```

## 与 Capability Output Contract 的关系

memory_save 可以保存 capability contract 的摘要：

```text
capability
status
output_ref
summary
tags
artifact_refs
```

memory_retrieval 返回 memory_context，供 planner / prompt_builder / response_composer 使用。

## 验收标准

- MemoryItem / MemoryQuery / MemorySearchResult 结构稳定。
- InMemoryStore / JsonlMemoryStore 符合同一接口。
- 不保存大文件本体。
- 不保存 API Key。
- 默认测试离线。
