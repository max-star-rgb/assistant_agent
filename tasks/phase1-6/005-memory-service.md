# Task 005：MVP 记忆服务

## Goal

实现本地可测试的记忆写入与检索能力，支持会话记忆、视频记忆、商品记忆、偏好记忆。

## Read first

- `docs/06-memory-design.md`
- `docs/03-agent-state.md`

## Scope

新增/修改：

```text
src/multimodal_agent/memory/store.py
src/multimodal_agent/memory/retriever.py
tests/unit/test_memory.py
```

## Steps

1. 实现 `MemoryStore` 接口。
2. 实现内存版 `InMemoryStore`。
3. 可选实现 JSONL 持久化。
4. 实现关键词/tags 检索。
5. 按 user_id 隔离记忆。

## Acceptance

```bash
pytest tests/unit/test_memory.py
```

必须验证：

- 保存记忆。
- 按 user_id 检索。
- 不跨用户泄漏。
- 关键词能命中“黑色包”“日系风格”等记忆。

## Out of scope

- 不接向量库。
- 不接真实数据库。
