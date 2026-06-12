# Task 023 接入可配置持久化 MemoryStore

## Goal

把 JSONL MemoryStore 接入 Agent Runtime，支持可配置的本地长期记忆。

## Read first

- `docs/20-memory-runtime-integration.md`
- `src/multimodal_agent/memory/store.py`
- `src/multimodal_agent/memory/jsonl_store.py`
- `src/multimodal_agent/config.py`
- 当前 graph runtime

## Scope

实现 MemoryStore 配置与 runtime 注入。

## Requirements

- 支持 `MULTIMODAL_AGENT_MEMORY_BACKEND=memory|jsonl`。
- 支持 `MULTIMODAL_AGENT_MEMORY_PATH`。
- 默认 backend 仍为 memory，避免污染开发环境。
- Graph runtime 可注入 memory_store。
- load_memory_node / save_memory_node 或等价逻辑接入 graph。
- 测试中使用 `tmp_path`。

## Tests

新增或更新：

```text
tests/test_memory_runtime_integration.py
```

覆盖：

- 默认 in-memory backend。
- jsonl backend 可写入。
- 新 runtime 实例能读取旧 jsonl memory。
- Agent response 可利用 memory_context。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 024。
