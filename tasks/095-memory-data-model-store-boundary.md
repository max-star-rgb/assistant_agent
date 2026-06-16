# Task 095 Memory Data Model and Store Boundary

## Goal

统一 memory 数据模型和 MemoryStore interface。

## Read first

- `docs/100-memory-data-model-and-store-boundary.md`
- 当前 memory schemas
- 当前 memory store
- 当前 memory tools
- 当前 capability output contract

## Requirements

- 定义或整理 MemoryItem。
- 定义或整理 MemoryQuery。
- 定义或整理 MemorySearchResult。
- 定义 MemoryStore interface。
- InMemoryStore / JsonlMemoryStore 符合同一接口。
- Memory 不保存大文件本体，只保存引用。
- memory_save 可保存 capability contract 摘要。
- memory_retrieval 返回 memory_context。
- 不调用外部 memory service。

## Tests

新增或更新：

```text
tests/test_memory_data_model.py
tests/test_memory_store_boundary.py
tests/test_memory_capability_contract_integration.py
```

覆盖：

- save/search/get/delete。
- InMemoryStore。
- JsonlMemoryStore。
- artifact refs。
- no raw media.
- no API Key.

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 096。
