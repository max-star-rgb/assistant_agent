# Task 098 Memory Privacy and User Isolation

## Goal

确保 memory search / save / delete 都严格按 user_id 隔离，并对敏感内容脱敏。

## Read first

- `docs/103-memory-privacy-user-isolation.md`
- 当前 memory store
- 当前 provider safety redaction
- 当前 tests for user/session

## Requirements

- 所有 memory 操作带 user_id。
- search 不跨 user_id。
- delete 不跨 user_id。
- session_id 用于当前会话优先。
- 写入前调用 redaction policy。
- sensitive memory 默认不自动保存，或标记 sensitivity。
- trace/log 不输出完整 memory content。
- 不保存 API Key / Bearer / Authorization / base64 / raw provider response。

## Tests

新增或更新：

```text
tests/test_memory_user_isolation.py
tests/test_memory_privacy_redaction.py
tests/test_memory_sensitive_policy.py
```

覆盖：

- user A cannot see user B memory。
- user A cannot delete user B memory。
- sensitive text redacted。
- memory_context no secret。
- trace no full sensitive memory。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 099。
