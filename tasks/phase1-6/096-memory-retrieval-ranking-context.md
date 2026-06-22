# Task 096 Memory Retrieval Ranking and Context Builder

## Goal

改进 memory retrieval 排序和 memory_context 构造。

## Read first

- `docs/101-memory-retrieval-ranking-context.md`
- 当前 memory retrieval tool
- 当前 planner
- 当前 prompt_builder
- 当前 response composer

## Requirements

- 支持 top_k。
- 支持 memory_type filter。
- 支持 tag filter。
- 支持 session_id filter。
- 支持 recency ranking。
- 支持 context max chars。
- 支持不同 capability 的 memory type priority。
- memory_context 可注入 planner / prompt_builder。
- 不接向量数据库。
- 不调用外部服务。

## Tests

新增或更新：

```text
tests/test_memory_retrieval_ranking.py
tests/test_memory_context_builder.py
tests/test_memory_context_in_planner.py
```

覆盖：

- top_k。
- type filter。
- recency。
- session priority。
- preference memory priority。
- context length cap。
- memory to image_generation。
- memory to render_3d。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 097。
