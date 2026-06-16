# 100 Memory Data Model and Store Boundary

## 目标

统一 memory 数据模型和 store 边界，让 memory_retrieval / memory_save 不再只是 mock 工具，而是可被 planner、response composer、demo runner 可靠使用的本地能力。

## MemoryItem

已整理为稳定 Pydantic schema：

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

`content`、`tags`、`artifact_refs` 和 `summary` 会拒绝敏感密钥、Authorization/Bearer/cookie/password/secret/token、完整 base64、raw media、raw provider response 等字段或内联媒体 data URL。

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

统一返回结构：

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

`InMemoryStore` 和 `JsonlMemoryStore` 均实现该接口。`JsonlMemoryStore` 仅保留旧式 `search(user_id=..., query=...) -> list[MemoryItem]` 作为兼容入口，新代码应使用 `search(MemoryQuery(...)) -> MemorySearchResult`。

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

从 capability contract 写入 memory 时，只保存公开摘要字段：

```text
capability
status
output_ref
summary
artifact_refs
tags
```

不保存 provider raw response、完整媒体、完整 base64 或密钥。

## 验收标准

- MemoryItem / MemoryQuery / MemorySearchResult 结构稳定。
- InMemoryStore / JsonlMemoryStore 符合同一接口。
- 不保存大文件本体。
- 不保存 API Key。
- 默认测试离线。
