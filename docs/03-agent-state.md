# 03 AgentState 与任务状态

## 1. AgentState 的作用

AgentState 是贯穿一次用户请求的唯一状态对象。它记录输入、感知结果、记忆上下文、意图、计划、工具调用、工具结果和最终响应。

## 2. 推荐字段

```python
class AgentState(BaseModel):
    run_id: str
    user_id: str
    session_id: str
    request: UserRequest

    memory_context: list[MemoryItem] = []
    perception: PerceptionBundle | None = None
    intent: IntentResult | None = None
    plan: TaskPlan | None = None

    selected_tools: list[ToolSelection] = []
    tool_calls: list[ToolCallRecord] = []
    tool_results: list[ToolResult] = []

    response: AgentResponse | None = None
    errors: list[AgentError] = []
    status: Literal["created", "running", "waiting_user", "completed", "failed"] = "created"
```

## 3. 状态更新原则

- 不在多个变量里重复维护同一事实。
- 每次工具调用都必须追加 ToolCallRecord。
- Agent 不丢弃失败记录，失败也要进入 `errors` 和 `tool_calls`。
- 可恢复任务要保存 `pending_question` 和 `missing_slots`。

## 4. IntentResult

```python
class IntentResult(BaseModel):
    intent: Literal[
        "chat",
        "understand_image",
        "understand_video",
        "search_product",
        "compare_price",
        "generate_image",
        "render_3d",
        "retrieve_memory",
        "save_memory",
        "multi_tool_task",
        "ask_followup",
    ]
    confidence: float
    missing_slots: list[str]
    rationale: str
```

## 5. TaskPlan

```python
class TaskStep(BaseModel):
    step_id: str
    action: str
    tool_name: str | None = None
    input_refs: list[str] = []
    depends_on: list[str] = []

class TaskPlan(BaseModel):
    goal: str
    steps: list[TaskStep]
    requires_followup: bool = False
    followup_question: str | None = None
```

## 6. ToolCallRecord

```python
class ToolCallRecord(BaseModel):
    call_id: str
    tool_name: str
    input: dict
    status: Literal["pending", "running", "succeeded", "failed"]
    started_at: datetime
    finished_at: datetime | None = None
    output_ref: str | None = None
    error_message: str | None = None
```
