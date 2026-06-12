# Task 018 记忆持久化

## Goal

把当前 in-memory memory 扩展为可持久化 MemoryStore。

## Read first

- `docs/14-memory-persistence.md`
- 当前 memory service 实现
- 当前 memory schemas

## Scope

实现本地持久化 MVP。

优先选择：

```text
JsonlMemoryStore
```

而不是直接上数据库。

## Requirements

- 定义 MemoryStore 接口或 Protocol。
- 保留 InMemoryStore。
- 新增 JsonlMemoryStore。
- 支持 save/search。
- 重启后仍可读取已保存记忆。
- 不引入外部数据库依赖。

## Tests

新增：

```text
tests/test_memory_persistence.py
```

覆盖：

- save 后文件存在。
- 新 store 实例可以读取旧 memory。
- search 能返回相关记录。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 019。
