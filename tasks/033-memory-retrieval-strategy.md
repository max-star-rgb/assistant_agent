# Task 033 Memory 检索策略增强

## Goal

增强 Memory 检索，支持类型过滤、Top-K、格式化 MemoryContext。

## Read first

- `docs/30-memory-retrieval-strategy.md`
- 当前 memory store
- 当前 AgentGraphRuntime memory 集成

## Requirements

- 扩展 MemoryQuery。
- 支持 memory_types 过滤。
- 支持 top_k。
- 支持 user_id/session_id 过滤。
- 支持 memory_context formatter。
- 不引入 Vector DB。
- 不调用 LLM 总结。

## Tests

新增或更新：

```text
tests/test_memory_retrieval_strategy.py
```

覆盖：

- 类型过滤。
- top_k。
- JSONL 跨实例检索。
- memory_context 字符长度限制。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 034。
