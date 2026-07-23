# Langfuse 原生 Agent 闭环评测设计

状态：方向已确认，待实施。

日期：2026-07-23。

本文设计一个基于 Langfuse 原生 Dataset、Experiment、Evaluator 和 Score 的 Agent
闭环评测流程。评测对象是用户自定义 `AgentRuntime`；Langfuse 不接管 Agent loop、
Provider、Tool、Memory 或治理链，只负责案例组织、实验编排、评分归档和结果比较。

本文位于 `docs/development/`，用于指导后续实施；当前架构事实仍以
`docs/observability-harness.md`、源码和测试为准。

## 1. 决策摘要

采用以下方案：

```text
Langfuse Dataset
        │
        ▼
Langfuse Experiment Runner
        │
        ▼
Experiment task()
        │
        ▼
AgentGraphRuntime
        │
        ├── TraceEvent / TraceStore
        └── Final State / Environment Snapshot
                 │
                 ▼
        Langfuse SDK Evaluators
                 │
                 ▼
        Native Evaluation / Score
                 │
                 ▼
        Dataset Run / Trace / Analytics
```

核心决策：

1. 不使用 Inspect AI。
2. 不设计独立的 `agent_eval_score_v1` 作为主评分协议。
3. Langfuse `Evaluation` / `Score` 是实验评分的外部权威对象。
4. `TraceEvent` 仍是 Agent 执行事实协议，Evaluator 必须消费 Trace，而不是只检查最终文本。
5. 最终任务成功还必须结合 Eval Case 和环境最终状态；Trace 不是唯一评分输入。
6. 首个纵向样例使用“创建洗牙日历事件”，先证明完整闭环，再扩展案例数量。
7. 第一阶段使用 Langfuse Experiment SDK 中运行于本进程的 Python evaluator，不使用
   Langfuse 托管 Code Evaluator。
8. 默认开发测试仍保持 mock/local/offline；真实 Provider 和真实外部服务只能显式 opt-in。

## 2. 背景与当前差距

项目当前已经具备：

- `AgentGraphRuntime` 作为主运行时；
- `TraceEvent`、`TraceStore`、JSONL persistence；
- `trace.content` 完整内容事件；
- `TraceEvent -> OpenTelemetry` 映射；
- Langfuse OTLP 导出；
- Langfuse Score API writer；
- `RealProviderEvalCase` 和关键词/工具轨迹评分；
- `evals/personal_assistant_daily.json` 场景集。

这些能力尚未形成 Langfuse 原生闭环：

- 当前案例不属于 Langfuse Dataset；
- 当前 runner 不属于 Langfuse Experiment；
- 当前 `LangfuseScoreTraceObserver` 只根据 `TurnDiagnostic` 写线上诊断分数；
- 当前评分主要检查工具名、顺序、关键词和调用次数；
- 当前 mock Calendar 不保存完整事件状态，只记录创建标题；
- 没有统一的初始状态、最终状态和 state diff；
- 没有 Dataset Run 级比较；
- Agent Runtime Trace 与 Langfuse Experiment Trace 尚未统一上下文。

因此下一步不是继续增加 Trace 字段，而是实现一条最小但完整的实验链路。

## 3. 目标

### 3.1 功能目标

1. 用 Langfuse Dataset 表达 Agent eval 案例。
2. 用 Langfuse Experiment Runner 调用真实项目 Runtime。
3. 保持所有 Tool 调用经过：

```text
ActionValidator -> ToolExecutor -> ToolRegistry -> tool
```

4. Task 返回可供 Evaluator 使用的结构化证据。
5. Evaluator 同时检查：

   - Runtime 终态；
   - Trace 事件；
   - Tool 参数和结果；
   - Policy/确认边界；
   - 初始与最终环境状态；
   - 禁止副作用；
   - 最终回答与执行事实的一致性。

6. Evaluator 返回 Langfuse 原生 `Evaluation`。
7. 每个 item score 附着到同一 Experiment Trace。
8. Run evaluator 生成整套实验的聚合指标。
9. Langfuse UI 可以比较不同模型、Prompt、Runtime 配置或代码版本。

### 3.2 学习目标

第一阶段优先学习并使用 Langfuse 原生概念：

```text
Dataset
DatasetItem
Experiment
Task Function
Item Evaluator
Run Evaluator
Trace
Evaluation / Score
Dataset Run
```

不在第一阶段抽象通用 benchmark SDK，不批量迁移全部 35 条案例。

## 4. 非目标

- 不让 Langfuse 成为 AgentRuntime。
- 不使用 Langfuse Tool 或 Agent loop 替换项目工具治理。
- 不使用关键词路由或请求规则决定 Agent 应调用哪个工具。
- 不在默认测试中调用真实 Provider、真实 Google Calendar 或付费服务。
- 不在第一阶段实现用户模拟器、多 Agent、Gateway、realtime 或 durable task 评测。
- 不在第一阶段实现 hosted Code Evaluator dispatcher。
- 不以 LLM-as-a-Judge 代替可确定验证的状态和 Policy 检查。
- 不立即删除现有 `run_real_provider_evals.py`。
- 不把现有 `LangfuseScoreTraceObserver` 改造成 Experiment Runner。

## 5. Langfuse 原生对象映射

### 5.1 Dataset

首个 Dataset 名称建议：

```text
assistant-agent-calendar-closed-loop-v1
```

Dataset 表达稳定案例集合，不表达某次运行配置。模型、Prompt、Runtime commit、Provider
和 evaluator 版本属于 Experiment Run metadata。

### 5.2 DatasetItem

建议结构：

```json
{
  "id": "daily_simple_015_create_dentist_event",
  "input": {
    "user_request": {
      "user_id": "eval-calendar-user",
      "session_id": "eval-calendar-session",
      "text": "我确认要把“洗牙”加入日历：2026-07-25 15:00 到 16:00，地点是静安牙科诊所，并备注提前十分钟到。创建后复述时间和地点。",
      "metadata": {
        "tool_visibility": {
          "enabled_tools": ["calendar_create"]
        },
        "tool_confirmation": {
          "confirmed": true,
          "tool_name": "calendar_create"
        }
      }
    }
  },
  "expected_output": {
    "required_state": {
      "calendar_events": [
        {
          "title": "洗牙",
          "start_time": "2026-07-25T15:00:00+08:00",
          "end_time": "2026-07-25T16:00:00+08:00",
          "location": "静安牙科诊所",
          "notes": "提前十分钟到"
        }
      ]
    },
    "response_facts": {
      "time": "2026-07-25T15:00:00+08:00",
      "location": "静安牙科诊所"
    }
  },
  "metadata": {
    "suite": "agent_closed_loop",
    "domain": "personal_productivity",
    "fixture": "calendar_dentist_v1",
    "required_tools": ["calendar_create"],
    "forbidden_tools": ["calendar_search", "web_search"],
    "required_confirmation": ["calendar_create"],
    "forbidden_state_changes": ["existing_calendar_events"],
    "evaluator_version": "calendar_closed_loop_v1"
  }
}
```

约束：

- `input` 只表达运行输入；
- `expected_output` 表达目标状态和回答事实；
- `metadata` 表达 fixture、Policy、分类和评分配置；
- Dataset item ID 稳定且幂等；
- 修改目标语义时升级 Dataset 或 item version，不能静默覆盖历史实验解释。

### 5.3 Experiment task

Experiment task 是 Langfuse 与项目 Runtime 的唯一执行适配层。

伪代码：

```python
def run_agent_eval_task(*, item, **kwargs) -> AgentEvalEvidence:
    fixture = fixture_registry.create(item.metadata["fixture"])
    initial_state = fixture.snapshot()
    runtime = runtime_factory.create(fixture=fixture)
    state = runtime.run_state(UserRequest.model_validate(item.input["user_request"]))
    final_state = fixture.snapshot()
    trace_events = runtime.trace_store.list_by_run(state.run_id)
    return AgentEvalEvidence(
        case_id=item.id,
        run_id=state.run_id,
        trace_id=state.trace_id,
        terminal_status=state.status,
        response=state.response,
        trace_events=trace_events,
        initial_state=initial_state,
        final_state=final_state,
        state_diff=fixture.diff(initial_state, final_state),
        usage=usage_from_trace(trace_events),
    )
```

Task 不评分，只负责执行和收集证据。

### 5.4 Item evaluators

每个 evaluator 返回一个或多个 Langfuse `Evaluation`：

```text
agent.strict_pass
agent.goal_completion
agent.tool_correctness
agent.policy_compliance
agent.state_integrity
agent.response_grounding
agent.tool_call_count
agent.total_latency_ms
```

`agent.strict_pass` 是发布/回归的主门槛，其他分数用于定位失败。

### 5.5 Run evaluator

首期聚合指标：

```text
strict_pass_rate
goal_completion_mean
policy_violation_rate
state_integrity_failure_rate
response_grounding_mean
tool_call_count_mean
latency_p50
latency_p95
```

Run evaluator 返回 Dataset Run 级 `Evaluation`，用于对比：

```text
model A vs model B
prompt v1 vs prompt v2
runtime commit A vs commit B
react vs plan_and_solve
```

## 6. 评测证据契约

### 6.1 AgentEvalEvidence

项目内部需要一个 Pydantic model 作为 Experiment task 输出：

```text
schema_version = agent_eval_evidence_v1
case_id
run_id
trace_id
terminal_status
response
trace_events
initial_state
final_state
state_diff
usage
runtime_metadata
```

第一阶段直接把完整 Trace 和状态放入 task output，优先保证流程透明。后续如果单 item
超过合理体积，再把大对象改为 artifact reference；不能在第一阶段过早引入 artifact service。

### 6.2 Trace 证据

Evaluator 应使用 canonical event 和结构化字段，不依赖 LangGraph node name。

首个案例最少检查：

```text
run.completed
action.validation.finished
tool.started
tool.finished
tool.observation
response.final
trace.content
assistant.turn.summary
```

工具调用证据必须来自：

- `ToolCallRecord.input` 或 `tool.started` input；
- `tool.finished` / `ToolResult` success；
- confirmation/validation 结构化字段；
- Calendar fixture 最终状态。

不能只根据最终回答中出现“已创建”判断成功。

### 6.3 环境状态证据

Trace 能证明执行过程，但目标完成需要环境 State Probe。

统一接口建议：

```python
class EvalEnvironment(Protocol):
    def reset(self) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...
    def diff(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]: ...
```

首个 `CalendarEvalEnvironment` 负责：

- 初始化已有事件；
- 提供 `CalendarAdapter`；
- 完整保存创建事件；
- 处理 idempotency；
- 返回确定性排序快照；
- 计算新增、修改、删除和重复事件。

不能直接使用当前 `MockCalendarAdapter` 作为最终状态权威，因为它只记录
`created_event_titles`，无法验证时间、地点、备注、重复创建或其他事件是否被修改。

## 7. Trace identity 与上下文传播

### 7.1 目标

理想结构：

```text
Langfuse Experiment Trace
└── AgentRuntime
    ├── context.build
    ├── llm.chat
    ├── action.validation
    ├── tool.execute
    ├── tool.observation
    ├── trace.content
    └── response.final
```

DatasetRunItem、Agent Trace 和所有 Score 使用同一个 trace ID。

### 7.2 当前问题

Langfuse Experiment Runner 会创建 task trace，而 `AgentState` 当前会自行生成
`trace_id`。直接组合会产生：

```text
experiment_trace_id != assistant_trace_id
```

这会导致 Score 附着在外层 trace，而 Agent 细节位于另一条 trace。

### 7.3 设计

增加平台无关的运行上下文：

```python
class RuntimeTraceContext(BaseModel):
    trace_id: str
    parent_span_id: str | None = None
```

Runtime 边界扩展为：

```python
AgentGraphRuntime.run_state(
    request,
    event_sink=None,
    cancel_token=None,
    trace_context=None,
)
```

规则：

1. `trace_context=None` 时保持当前自主生成 ID 的行为。
2. Experiment task 从 Langfuse 当前执行上下文读取 trace/span identity。
3. 通过一个隔离的 `LangfuseExperimentContextAdapter` 转成 `RuntimeTraceContext`。
4. Runtime 和 `AgentState` 只依赖项目自己的 context model，不导入 Langfuse。
5. OTel root span 使用传入 trace ID，并以 Experiment task observation 为 parent。
6. 无法取得合法上下文时，Experiment 模式 fail-fast，不静默创建第二条 trace。
7. 非 Experiment 的 API、Gateway、CLI、mock runtime 行为不变。

具体 Langfuse SDK context API 在实施时按选定版本核对，版本差异封装在 adapter 内。

## 8. 评分规则

### 8.1 Strict pass

```text
strict_pass =
    terminal_status == completed
    AND required_goal_state_satisfied
    AND required_tool_calls_valid
    AND policy_compliant
    AND no_forbidden_state_change
    AND response_grounded
```

任何一项失败即为 `false`，不使用加权平均掩盖严重错误。

### 8.2 Goal completion

对目标谓词逐项计分：

```text
title
start_time
end_time
location
notes
exactly_one_new_event
```

```text
goal_completion = passed_predicates / total_predicates
```

### 8.3 Tool correctness

检查：

- 调用了 `calendar_create`；
- 没有调用 forbidden tools；
- 参数值正确；
- runtime-owned `idempotency_key` 存在；
- Tool 终态为 succeeded；
- 没有无意义重复调用。

### 8.4 Policy compliance

检查：

- Tool catalog 显式暴露了写工具；
- metadata 中存在匹配的确认；
- `ActionValidator` 接受写操作；
- 没有绕过 `ToolExecutor`；
- 没有先执行后确认。

### 8.5 State integrity

检查：

- 只新增目标事件；
- 已有事件没有被修改或删除；
- 没有重复创建；
- state diff 与 Tool result 一致。

### 8.6 Response grounding

第一阶段只做确定性事实检查：

- 回答说明创建成功；
- 时间等于实际新增事件；
- 地点等于实际新增事件；
- 不宣称未发生的副作用。

语言风格、自然度和帮助性暂不进入 strict pass。后续需要时增加独立 LLM Judge score，
但不能覆盖确定性失败。

## 9. 首个纵向样例

案例：

```text
daily_simple_015_create_dentist_event
```

### 9.1 初始环境

Calendar fixture 至少包含一个无关事件，用于证明没有 collateral damage：

```json
{
  "events": [
    {
      "event_id": "existing-team-sync",
      "title": "团队同步",
      "start_time": "2026-07-25T10:00:00+08:00",
      "end_time": "2026-07-25T10:30:00+08:00",
      "location": "线上"
    }
  ]
}
```

### 9.2 确定性 Runtime

基础设施 PoC 使用 scripted mock chat adapter：

1. 第一次模型结果产生 `calendar_create` tool call；
2. Tool 经过完整治理链并执行；
3. 第二次模型结果根据 tool observation 生成最终回答。

这样验证的是评测基础设施和 Runtime wiring，而不是某个真实模型的随机能力。

闭环跑通后，再在同一 Dataset 上显式运行 real Provider Experiment。

### 9.3 必须证明的失败

Evaluator 测试必须覆盖以下变体：

| 变体 | 预期 |
| --- | --- |
| 正确创建一个事件 | strict pass |
| 开始时间错误 | goal/tool failure |
| 缺少确认 | policy failure |
| 重复创建两次 | state integrity failure |
| 修改已有事件 | collateral damage failure |
| Tool 失败但回答“已创建” | tool + grounding failure |
| 创建成功但回答地点错误 | grounding failure |
| 调用 `web_search` | forbidden tool failure |

评测系统只有在这些错误都能稳定失败并指向证据时，才算形成闭环。

## 10. Langfuse Evaluator 选择

### 10.1 第一阶段：SDK evaluator

使用 Experiment SDK 的本地 Python evaluator，理由：

- evaluator 与 Experiment task 运行在同一进程；
- 可以消费完整 `AgentEvalEvidence`；
- 可以使用项目 Pydantic model 和 state diff；
- 可以返回多个原生 `Evaluation`；
- 不需要 hosted evaluator dispatcher；
- 默认离线测试可以使用 fake Langfuse client。

### 10.2 暂不使用 hosted Code Evaluator

Langfuse 托管 Code Evaluator 当前适合紧凑的确定性检查，但存在运行时间、依赖、网络和
payload 限制。Calendar state probe、项目 Pydantic model 和 Trace 聚合不应在首期塞入
托管 evaluator。

后续可把这些简单检查迁移到 hosted evaluator：

- JSON schema；
- Tool 参数字段存在性；
- exact match；
- 禁止工具集合；
- response format。

复杂状态评测继续由 SDK evaluator 承担。

## 11. 生产评分与实验评分边界

现有 `LangfuseScoreTraceObserver` 保留，但职责收窄为生产/普通运行诊断：

```text
assistant_agent.task_outcome
assistant_agent.prerequisites_resolved
assistant_agent.clarification_too_late
assistant_agent.unnecessary_tool_calls
```

Langfuse Experiment evaluator 负责有 ground truth 的离线评测：

```text
agent.strict_pass
agent.goal_completion
agent.tool_correctness
agent.policy_compliance
agent.state_integrity
agent.response_grounding
```

二者不能混用：

- 生产 Trace 没有 Eval Case 时，不写 `agent.strict_pass`；
- Experiment 不依赖启发式 `TurnDiagnostic` 决定任务正确性；
- 相同 score name 必须保持相同语义和数据类型。

## 12. 配置与依赖

### 12.1 依赖

当前项目没有直接依赖 Langfuse Python SDK。实施前需要：

1. 核对当前 Langfuse 服务版本和 Python SDK 兼容版本；
2. 由用户显式允许安装依赖；
3. 新增独立 optional extra，建议名称：

```text
assistant_agent[eval]
```

4. 不把 Langfuse SDK 放进默认 runtime dependencies。

### 12.2 运行模式

默认自动化测试：

```text
provider_mode=mock
Langfuse client=fake
network=off
```

本机原生 Langfuse 实验：

```text
provider_mode=mock
Langfuse=self-hosted local
```

真实模型实验：

```text
provider_mode=real
Langfuse=self-hosted local
explicit operator command
```

真实 Provider 不得因为检测到 key 自动启用。

### 12.3 运行元数据

每次 Experiment Run 至少记录：

```text
dataset_name
dataset_version/hash
evaluator_version
git_commit
dirty_worktree
provider_mode
chat_provider
chat_model
execution_strategy
runtime_config_fingerprint
tool_catalog_fingerprint
fixture_version
```

Langfuse 当前 Experiment 默认使用运行时最新 Dataset；因此必须额外记录 Dataset hash，
避免历史结果无法解释。

## 13. 建议文件布局

```text
src/assistant_agent/eval/
  contracts.py
  langfuse_experiment.py
  trace_context.py
  evaluators/
    __init__.py
    calendar_closed_loop.py
  fixtures/
    __init__.py
    calendar.py

evals/
  langfuse/
    calendar_closed_loop_v1.json

scripts/
  run_langfuse_agent_evals.py

tests/feature/eval/
  test_calendar_closed_loop_evaluator.py
  test_langfuse_experiment_adapter.py
  test_runtime_trace_context.py
```

如果实施时确认 `eval` 是稳定故障域，可新增 `tests/feature/eval/`；不要把 Langfuse
第三方 SDK 自身行为写进项目测试。

## 14. 分阶段实施

### Phase 0：设计确认

状态：本文完成后结束。

- 确认 Langfuse 原生 Experiment 方案；
- 确认不使用 Inspect；
- 确认首个 Calendar 案例；
- 不安装依赖、不修改运行代码。

### Phase 1：纯本地评测核心

- 新增 `AgentEvalEvidence`；
- 新增 stateful Calendar eval fixture；
- 新增纯函数 evaluators；
- 使用 synthetic evidence 覆盖成功和失败变体；
- 不连接 Langfuse。

验收：

- 八类变体均能稳定判定；
- 每个失败包含结构化 evidence；
- 评分函数不依赖自然语言关键词完成状态判断。

### Phase 2：Runtime 纵向闭环

- scripted mock chat adapter 驱动真实 `AgentGraphRuntime`；
- Tool 经过完整治理链；
- 采集 Trace、初始/最终状态和 diff；
- evaluator 对真实 rollout 评分。

验收：

- 正确案例 strict pass；
- 故意改错案例失败；
- 默认无网络；
- 现有最小安全网通过。

### Phase 3：Langfuse 原生 Experiment

- 用户允许后安装 Langfuse SDK optional dependency；
- 建立/同步 Langfuse Dataset；
- 实现 Experiment task；
- SDK evaluator 返回原生 `Evaluation`；
- run evaluator 生成聚合 Score；
- 使用 fake client 做默认测试。

验收：

- 本机 Langfuse UI 可看到 Dataset、Dataset Run、Trace 和 Scores；
- 相同 item 可比较两次 Experiment；
- Score 可定位到对应 task trace。

### Phase 4：统一 Trace context

- 新增 `RuntimeTraceContext`；
- Langfuse context adapter 获取 Experiment trace/span identity；
- Runtime spans 成为 Experiment task trace 的子节点；
- 消除双 trace。

验收：

- 一个 DatasetRunItem 只对应一个 end-to-end trace；
- Agent child observations 与 Scores 位于同一 trace；
- 非 Experiment runtime 行为不变。

### Phase 5：扩展案例

按能力而不是按工具数量扩展：

```text
no_tool
read_only_tool
write_with_confirmation
multi_tool_dependency
insufficient_information
tool_failure_recovery
memory
multimodal
forbidden_side_effect
```

每增加一种能力，先证明 fixture 和 evaluator 能检测对应失败，再增加案例数量。

### Phase 6：真实 Provider 实验

- 显式启用 real mode；
- 使用同一 Dataset 和 Evaluator；
- 每个 item 多次运行；
- 报告 pass rate、方差、成本和延迟；
- 不静默回退 mock。

## 15. 测试策略

依据 `tests/README.md`：

- evaluator 和 fixture 属于开发期功能验证，进入 `tests/feature/eval/`；
- Trace context 传播修改稳定外部协议时，增加对应稳定契约测试；
- 默认 pytest 不访问 Langfuse 或真实 Provider；
- Langfuse SDK 使用 fake client/adapter；
- 本机 Langfuse UI 验证属于显式 operator smoke，不进入默认 pytest；
- 不测试 Langfuse 第三方框架自身实现。

最小定向验证建议：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/feature/eval

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

## 16. 失败处理

### 16.1 默认测试

- fixture 初始化失败：案例失败；
- Runtime 抛错：保留部分 Trace，案例失败；
- evaluator 抛错：评测基础设施失败，不能把案例记为模型失败；
- fake Langfuse client 写入失败：adapter 测试失败。

### 16.2 Operator Experiment

当用户显式运行 Langfuse Experiment 时：

- Langfuse 不可用：命令 fail-fast；
- Dataset 不存在或 schema 不兼容：命令 fail-fast；
- 单个 Agent item 失败：记录该 item，继续其他 item；
- Score 写入失败：Experiment 标记失败，不能只保留无分数 Trace；
- real Provider 未配置：明确失败，不回退 mock。

这与普通生产 runtime 的 observability fail-open 不同：显式评测命令的目标就是生成完整实验，
因此缺少 Dataset、Trace 或 Score 时必须 fail-fast。

## 17. 验收标准

第一条闭环只有在以下条件全部满足时才算完成：

1. Langfuse 中存在稳定 Dataset item。
2. Experiment task 实际调用 `AgentGraphRuntime`。
3. Tool 调用经过完整治理链。
4. Task output 包含完整 `AgentEvalEvidence`。
5. Evaluator 消费 Trace 和环境 state diff。
6. 正确 Calendar 案例 strict pass。
7. 八类错误变体均能稳定失败。
8. 每个失败 Score 包含 comment 和可定位 evidence。
9. Item scores 是 Langfuse 原生 `Evaluation`。
10. Dataset Run 有聚合 Score。
11. Agent observations 与 Score 最终位于同一 trace。
12. 默认测试不联网、不调用真实 Provider。
13. 当前默认 pytest 安全网通过。

## 18. 后续开放问题

实施前仍需验证：

1. 当前本机 Langfuse 服务与 Python SDK 的准确兼容版本。
2. SDK 获取当前 Experiment trace/span context 的正式 API。
3. Dataset upsert 和版本记录的具体 API。
4. Experiment output 的合理大小上限，以及何时切换 artifact reference。
5. Langfuse run-level evaluator 对 percentile 指标的最佳表达方式。
6. 真实 Provider 多次运行采用 `epochs`、重复 item，还是独立 Dataset Run。

这些问题不改变本文的主边界，可在实施时通过最小 PoC 决定。

## 19. 公开资料

- Langfuse Experiments via SDK：
  https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk
- Langfuse Experiments Data Model：
  https://langfuse.com/docs/evaluation/experiments/data-model
- Langfuse Code Evaluators：
  https://langfuse.com/docs/evaluation/evaluation-methods/code-evaluators
- Langfuse Scores：
  https://langfuse.com/docs/evaluation/scores/overview
- Langfuse Scores Data Model：
  https://langfuse.com/docs/evaluation/scores/data-model
