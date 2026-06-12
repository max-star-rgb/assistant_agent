# 29 本地任务队列抽象设计

## 目标

为后续视频理解、图片生成、3D 渲染等耗时任务提供统一任务队列抽象，但 Phase 4 不强制引入 Redis/Celery。

## 为什么先做抽象

直接接 Celery/Redis 会增加部署复杂度。当前更重要的是把 Runtime 与任务执行机制解耦。

## 推荐接口

```python
class TaskQueue(Protocol):
    def submit(self, task: AgentTask) -> TaskHandle:
        ...

    def get_status(self, task_id: str) -> TaskStatus:
        ...

    def get_events(self, task_id: str) -> list[AgentEvent]:
        ...
```

## 实现顺序

```text
InlineTaskQueue
InMemoryTaskQueue
RedisTaskQueue
CeleryTaskQueue
```

Phase 4 只实现：

```text
InlineTaskQueue
InMemoryTaskQueue
```

## AgentTask

```python
class AgentTask(BaseModel):
    task_id: str
    user_id: str
    session_id: str
    request: UserRequest
    status: Literal["queued", "running", "success", "failed", "cancelled"]
```

## 与 WebSocket 的关系

```text
HTTP submit
  ↓
TaskQueue.submit()
  ↓
返回 task_id

WebSocket subscribe
  ↓
TaskQueue.get_events(task_id)
  ↓
推送事件
```

## 验收标准

- 可以提交一个 agent task。
- 可以查询 task status。
- 可以读取 task events。
- 不依赖外部服务。
