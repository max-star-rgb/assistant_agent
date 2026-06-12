# 14 记忆持久化设计

## 目标

将当前 in-memory memory 升级为可持久化记忆，为后续“上次那个商品”“之前的视频片段”“我喜欢的风格”提供基础。

## 记忆类型

```text
SessionMemory
VideoMemory
ProductMemory
PreferenceMemory
TaskMemory
```

## MVP 持久化方案

第一阶段不必直接上 PostgreSQL + Vector DB。

推荐顺序：

1. JSONL 本地持久化
2. SQLite
3. PostgreSQL
4. Vector DB / Embedding

## 推荐接口

```python
class MemoryStore(Protocol):
    def save(self, item: MemoryItem) -> MemoryItem:
        ...

    def search(self, query: MemoryQuery) -> list[MemoryItem]:
        ...
```

实现：

```text
InMemoryStore
JsonlMemoryStore
SqliteMemoryStore
VectorMemoryStore
```

## 与 AgentState 的关系

AgentState 不直接保存所有历史记忆。

正确流程：

```text
load_memory_node
  ↓
memory_context 注入 AgentState
  ↓
任务执行
  ↓
save_memory_node
```

## 验收标准

- 支持保存 memory item 到本地文件或 SQLite。
- 重启进程后仍能检索。
- 单元测试不依赖外部数据库。
- 记忆检索结果能注入 AgentState。
