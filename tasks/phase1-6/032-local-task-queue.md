# Task 032 本地任务队列抽象

## Goal

新增本地 TaskQueue 抽象，为后续异步任务和长任务执行做准备。

## Read first

- `docs/29-local-task-queue.md`
- 当前 runtime
- 当前 WebSocket event stream

## Scope

实现：

```text
InlineTaskQueue
InMemoryTaskQueue
TaskStatus
TaskHandle
AgentTask
```

## Requirements

- 不引入 Redis/Celery。
- 支持 submit。
- 支持 get_status。
- 支持 get_events。
- 可以与 EventSink 连接。
- 默认仍可同步执行。

## Tests

新增：

```text
tests/test_task_queue.py
```

覆盖：

- submit task。
- 查询状态。
- 读取事件。
- 任务失败状态。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 033。
