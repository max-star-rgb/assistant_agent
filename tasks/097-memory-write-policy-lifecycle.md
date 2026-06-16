# Task 097 Memory Write Policy and Lifecycle

## Goal

定义 memory 写入策略、生命周期和删除能力，避免无控制地保存所有内容。

## Read first

- `docs/102-memory-write-policy-and-lifecycle.md`
- 当前 save_memory_node
- 当前 memory_save tool
- 当前 runtime
- 当前 provider safety redaction

## Requirements

- 定义 MemoryWritePolicy。
- 默认不保存 raw media。
- 默认不保存 raw provider response。
- 默认不保存 API Key / Authorization。
- 显式“记住”请求写入 preference/product/task memory。
- task summary 可写入 memory。
- artifact output_ref 可写入 memory。
- 支持 expires_at 字段。
- 支持 delete by memory_id + user_id。
- 不跨用户删除。
- 不调用外部服务。

## Tests

新增或更新：

```text
tests/test_memory_write_policy.py
tests/test_memory_lifecycle.py
tests/test_memory_delete.py
```

覆盖：

- explicit remember。
- auto task summary。
- artifact output_ref。
- no raw media.
- no secrets.
- delete by user.
- expires_at field.

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 098。
