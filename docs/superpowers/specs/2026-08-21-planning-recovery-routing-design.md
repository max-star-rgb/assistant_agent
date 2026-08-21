# Planning Graph 统一恢复路由设计

日期：2026-08-21  
状态：待用户复核

## 1. 背景

当前 `AssistantPlanningGraph` 复用同一个 `AssistantFastAgent` 执行 planner、worker 与 finalizer。共享 Agent
统一装配 `ToolCallLimitMiddleware(run_limit=..., exit_behavior="error")`，planner 节点另外使用没有窄化
`retry_on` 的 `RetryPolicy(max_attempts=2)`。这使本应由 Graph 处理的预算耗尽变成 Python exception，并可能触发
整个 planner 节点重试。

真实 trace `01a02224-3ab1-7543-9d39-f560cfb59f59` 展示了这一问题：planner 已完成 8 次 Tool 调用，模型申请
第 9 次调用时触发 `ToolCallLimitExceededError(9/8)`；通用 planner retry 随后从原始输入重跑，第二次尝试又遇到
DashScope HTTP 400，最终 RootGraph 只呈现后一个 Provider 异常，第一次尝试已获得的 Tool evidence 也没有进入父图
state。

worker 当前只对明确 operational failure 做有限重试；耗尽后生成 failed `WorkerResult`，scheduler 将依赖它的节点
机械标记为 failed，随后直接进入 finalizer。成功 worker 不会重放，但 Graph 也没有根据失败缺口回到 planner 的动态
replan 路径。

## 2. 目标与非目标

### 2.1 目标

- 为 planner 与 worker 建立统一、显式、可 checkpoint 的恢复状态和确定性 recovery router。
- 把 Tool/model/attempt 预算耗尽从异常改为结构化 outcome，并保留同一次尝试已经完成的安全 evidence/result。
- worker 失败后允许在有界预算内回到 planner，生成替代计划。
- 已成功的 `WorkerResult` 默认冻结复用，不因 retry 或 replan 重放。
- 区分 operational、business、authorization、cancel/interrupt 和 contract bug，避免模型绕过权限或掩盖程序错误。
- 建立 phase、attempt、replan 和全图四层预算，防止动态恢复退化为无限循环。
- 使用 LangGraph 原生 StateGraph、`Send`、state reducer、checkpoint、interrupt 和 stream，不建立第二套 Runtime。
- 让恢复决策通过原生 `updates` 和安全 `custom` 事件可观察，并保留 subgraph namespace。

### 2.2 非目标

- 不保证 Studio Graph UI 自定义渲染任意 `custom` payload；完整事件仍由显式订阅的 SDK 客户端消费。
- 不让模型自由判定异常类别、权限边界、剩余预算或是否可以重放副作用。
- 不恢复旧 Runtime facade、repair ledger 服务、后台队列或外部 workflow supervisor。
- 不通过简单提高固定 Tool limit 代替恢复语义。
- 不在本次设计中改变 fast 模式、coding 模式或 Memory Graph 的业务行为。
- 不保存 Provider 原始响应、异常正文、Tool 参数、Tool 原始结果或敏感凭据。

## 3. 设计原则

1. **预期内失败是状态，不是异常。** 预算耗尽、无结果和可恢复执行失败必须形成严格 outcome。
2. **恢复决策确定性。** retry、replan、finalize、propagate 由本地分类器和预算决定，模型只提交候选计划。
3. **成功结果单调冻结。** 已成功结果不可被相同或后续 generation 覆盖；只有确定性过期/冲突规则可以废弃。
4. **权限只能收窄。** replan 不得扩大 Tool、Skill、reference grant、身份或副作用权限。
5. **恢复有界。** 每次 retry/replan 消耗明确预算；达到安全上限后进入受控 finalizer。
6. **并行仍由 LangGraph 调度。** scheduler 继续按 DAG wave 使用 `Send` 并行派发，不因恢复能力串行化健康节点。
7. **错误可解释但不泄漏。** checkpoint、prompt 和 stream 只携带稳定错误码与有界摘要。

## 4. 方案选择

采用显式 `RecoveryState + recovery_router`，不采用以下方案：

- 不把主要恢复逻辑隐藏在 `error_handler -> Command(goto=...)` 中。error handler 只负责把已经分类的执行失败转成
  outcome，不能拥有全局恢复策略。
- 不新增 supervisor Agent 或 supervisor 子图。现有 planner 已是唯一计划生成者，scheduler 是唯一确定性调度者；新增
  supervisor 会形成重复大脑。

## 5. 错误分类

所有恢复分类均由本地代码完成，分类器沿 exception cause/context 链检查，遇到任何不可恢复类型立即停止降级。

| category | 典型来源 | 行为 |
| --- | --- | --- |
| `budget_exhausted` | Tool/model/attempt 预算耗尽 | 保留局部成果，进入 recovery router |
| `operational` | timeout、连接失败、HTTP 408/409/425/429/5xx | 同节点有限 retry，耗尽后 replan |
| `business_failure` | Tool 明确无结果、严格 worker completion 报告 insufficient | 不机械重试，进入 replan |
| `authorization` | Permission、Skill/reference grant、HITL reject | 禁止模型绕过；按既有拒绝/interrupt 语义结束或传播 |
| `contract_bug` | schema、类型、断言、代码 invariant、未分类且不可重试的 HTTP 4xx | fail closed，不进入自愈 |

`GraphBubbleUp`、`GraphInterrupt`、cancel 和 `NodeCancelledError` 始终保持 LangGraph 原生传播。Provider HTTP 4xx
默认属于不可恢复边界；只有未来存在明确、可验证的业务错误码映射时，才允许逐码加入其他类别。

## 6. 数据模型

### 6.1 预算

```python
class BudgetUsage(BaseModel):
    model_calls: int = 0
    tool_calls: int = 0
    node_attempts: int = 0
    replans: int = 0
```

所有字段非负。合并只允许逐字段相加；scheduler 的 reservation 与实际 usage 分开保存，wave join 后再确定性核销。

### 6.2 失败事实

```python
class FailureFact(BaseModel):
    category: Literal[
        "budget_exhausted",
        "operational",
        "business_failure",
        "authorization",
        "contract_bug",
    ]
    code: str
    phase: Literal["planner", "worker", "finalizer"]
    plan_generation: int
    work_item_id: str | None = None
    attempt: int
```

`code` 来自受信静态枚举；模型、Provider body 和异常字符串不能直接成为 `code`。

### 6.3 Planner outcome

```python
class PlannerOutcome(BaseModel):
    status: Literal["succeeded", "budget_exhausted", "operational_failed"]
    plan_candidate: NativePlanProposal | None = None
    evidence_ids: tuple[str, ...] = ()
    failure: FailureFact | None = None
    usage: BudgetUsage
```

约束：

- `succeeded` 必须带 `plan_candidate` 且不带 failure。
- 非成功状态不得伪造 candidate。
- `evidence_ids` 只能引用本次或以前已经进入 state 的 `PlannerEvidence`。

### 6.4 Worker completion 与 outcome

worker phase 使用严格 structured response：

```python
class WorkerCompletion(BaseModel):
    status: Literal["completed", "insufficient"]
    content: str
```

模型只能报告业务完成度，不能报告 operational、authorization 或 contract 分类。

```python
class WorkerOutcome(BaseModel):
    execution_id: str
    plan_generation: int
    work_item_id: str
    attempt: int
    status: Literal[
        "succeeded",
        "budget_exhausted",
        "operational_failed",
        "business_failed",
    ]
    result: WorkerResult | None = None
    failure: FailureFact | None = None
    usage: BudgetUsage
```

`execution_id` 由 runtime 按 generation、node ID 和 attempt 确定性构造，模型不可写入。

### 6.5 Recovery decision

```python
class RecoveryDecision(BaseModel):
    action: Literal["retry", "replan", "finalize", "propagate"]
    reason_code: str
    source_execution_ids: tuple[str, ...] = ()
```

该值只由 `assess_planner` 或 `assess_workers` 产生。

## 7. State 与 reducer

`PlanningState` 增加：

- `plan_generation: int`；首次计划为 0，每次执行期 replan 加 1。
- `planner_outcome: PlannerOutcome | None`。
- `worker_outcomes: dict[str, WorkerOutcome]`。
- `frozen_worker_results: dict[str, WorkerResult]`。
- `superseded_work_item_ids: list[str]`，使用唯一字符串 reducer。
- `recovery_context: dict[str, JsonValue] | None`。
- `recovery_history: list[RecoveryDecision]`，只由顺序 recovery 节点追加并有固定上限。
- `budget_usage: BudgetUsage`。
- `wave_reservations: dict[str, BudgetUsage]`。

`worker_outcomes` 和 `frozen_worker_results` 使用确定性 dict reducer：

- 新 key 可以加入。
- 已存在 key 只有新旧值完全相等时视为幂等重放。
- 相同 key 的不同值立即触发 invariant error。
- 不使用 arrival order 或“最后一个覆盖”解决并行冲突。

`worker_results: Annotated[list[WorkerResult], operator.add]` 不再作为恢复期权威状态。迁移后，finalizer 与 scheduler
从 frozen results 和当前 generation outcomes 确定性派生有序结果。

所有 state 值必须 JSON-safe，不保存 exception、client、writer、task、future 或文件句柄。

## 8. Phase-aware budget middleware

共享 `AssistantFastAgent` 保持不变，不为 planner/worker 新建独立 Agent。现有统一
`ToolCallLimitMiddleware(exit_behavior="error")` 替换为 phase-aware middleware：

- 从受信 `agent_phase` 选择 composition 配置的 phase ceiling。
- fast、planner、worker 分别计数；finalizer 的 Tool projection 为空且 ceiling 为 0。
- 模型申请超限 Tool 时，为被阻止调用生成标准 error `ToolMessage`，保持消息协议闭合。
- middleware 写入结构化 `phase_budget_status=exhausted` 与 usage，并有界结束当前 FastAgent。
- 不为预期预算耗尽抛 `ToolCallLimitExceededError`。

因此 planner/worker 外层节点仍能读取本次 FastAgent 返回的 messages、成功 ToolMessage、usage 与预算状态。planner
先捕获新 `PlannerEvidence`，再形成 `PlannerOutcome`；不能先判断 candidate 缺失并抛异常。

phase ceiling 由受信 composition/config 提供，planner 输出不能自行提高。以现有
`config.max_tool_iterations` 记为基础额度 `B`，首版默认值明确为：

- fast：Tool `B`，model `B + 1`；
- planner：Tool `2B`，model `2B + 1`；
- worker：每个 work item 的 Tool `B`，model `B + 1`；
- finalizer：Tool `0`，model `1`；
- planner operational attempt 最多 `2` 次；
- worker operational attempt 保持现有最多 `3` 次；
- 执行期 replan 最多 `2` 次；
- 全图 Tool 调用上限 `8B`，model 调用上限 `10B`，node attempt 上限 `32`；
- recovery history 最多 `32` 条。

`B` 必须为正整数。派生 ceiling 必须在配置加载时校验，且单 phase allowance 不得超过剩余全图额度。上述值是安全
默认值而非模型可见建议；部署方可以通过受信配置收紧或提高，但不能在单次请求、planner proposal 或用户正文中覆盖。

## 9. Graph 拓扑

```text
START
  -> planner
  -> assess_planner
       success             -> admit_plan
       operational retry   -> planner
       budget/retry spent  -> prepare_replan
       recovery exhausted  -> controlled_finalize
       authorization       -> existing reject/interrupt semantics
       contract/cancel     -> propagate

admit_plan
  -> admitted             -> scheduler
  -> admission rejected   -> prepare_revision -> planner

scheduler
  -> reserve_wave_budget
  -> Send(worker...)
  -> join
  -> assess_workers
       healthy + pending       -> scheduler
       complete                -> finalizer
       operational retryable   -> retry failed workers
       replannable gap         -> prepare_replan
       recovery exhausted      -> controlled_finalize

prepare_replan
  -> planner

finalizer
  -> END

controlled_finalize
  -> END
```

planner 与 worker 的 LangGraph `RetryPolicy.retry_on` 必须窄化为 operational classifier。预算、business failure、
authorization、interrupt、cancel 和 contract bug 不得进入框架机械 retry。

## 10. Scheduler、冻结与 replan

### 10.1 正常 wave

`scheduler` 仍按 plan 顺序计算 ready nodes。派发前由 `reserve_wave_budget` 根据剩余全图额度为每个 worker 分配
最大 allowance；reservation 使用稳定 plan order，不能受并行完成顺序影响。额度不足以启动整个 ready wave 时，
只派发稳定前缀，其余留待下一轮；完全无法分配时进入 recovery assessment。

### 10.2 Worker retry

只有 operational failure 可以使用同一 `work_item_id` 和 generation 增加 attempt。retry 复用相同输入、冻结依赖和
权限投影，并消耗 node attempt 与全图预算。budget/business failure 不做同输入机械 retry。

### 10.3 Replan 准备

`prepare_replan` 是不调用模型的确定性节点：

1. 把当前及以前 generation 的成功 outcome 写入 `frozen_worker_results`。
2. 保持既有 frozen result 不变；重复写必须完全一致。
3. 把失败、受失败依赖阻塞和尚未执行的当前节点加入 superseded 集合。
4. 汇总未完成 deliverable、稳定 failure code、冻结结果摘要、PlannerEvidence 引用和剩余预算。
5. 生成有界、prompt-safe、`trust=runtime-fact` 的 recovery context。
6. 增加 `plan_generation` 和 replan usage。
7. 清除当前 candidate/outcome 的活动指针，但不删除历史 checkpoint 事实。

### 10.4 成功结果复用

成功 `WorkerResult` 默认永久冻结，replan 不得要求它重新运行。新计划节点可以通过显式依赖引用冻结结果。只有未来新增
确定性过期或冲突规则时才能废弃冻结结果；模型自然语言不得直接触发废弃。

## 11. Recovery plan schema 与 admission

`NativePlanProposal` 升级 schema version。`NativePlanNode` 增加：

```python
replaces_node_ids: tuple[str, ...] = ()
frozen_dependency_ids: tuple[str, ...] = ()
```

`PlanDeliverable` 增加显式 `frozen_result_refs: tuple[str, ...] = ()`，不把历史结果伪装成当前 generation 的 producer
node。

首次计划必须令该字段为空。replan admission 额外验证：

- 新 node ID 在整个 run 的所有 generation 中唯一。
- `replaces_node_ids` 只能引用失败、阻塞或未执行节点。
- 禁止替换成功 frozen result。
- 每个被替代节点最多由一个新节点声明替代。
- `frozen_dependency_ids` 引用的 frozen result 必须存在且成功；普通 `depends_on` 仍只能引用当前 generation 节点。
- deliverable 必须通过 `frozen_result_refs`、PlannerEvidence 或当前 generation producer node 重新闭合。
- required Skill 必须来自已激活快照；Tool allowlist 与 reference grant 只能保持或收窄。
- 计划的最小预算需求不得超过 scheduler 剩余安全额度。
- DAG 仍满足现有节点数量、深度、无环和引用约束。

admission revision 与执行期 replan 分开计数。纯 schema/admission 修订不增加 generation；一旦 worker 开始执行，后续计划
变化必须进入新 generation。

## 12. Finalizer 与受控终态

正常 finalizer 继续复用共享 FastAgent，但确定性清空 Tool 与 structured response。输入只包含：

- 原始请求的有界投影；
- 当前 deliverables；
- PlannerEvidence；
- 按业务顺序排列的 frozen/current 成功结果；
- 尚未解决的稳定 FailureFact；
- 明确的裁剪与验证状态。

finalizer operational failure 可以有限 retry。retry 耗尽、全图预算耗尽或已无安全 model call 额度时进入
`controlled_finalize`。该节点不调用 Tool；在无法调用模型时构造标准 `AIMessage`，机械说明已完成项、缺失项和稳定
错误码，不虚构业务答案，保证可恢复失败不会令 RootGraph 无消息终止。

authorization、cancel/interrupt 与 contract bug 不得被 controlled finalizer 吞掉；它们继续按对应语义传播。

## 13. Stream 与观测

以下恢复节点自然通过 `updates` 暴露 state delta：

- `assess_planner`
- `assess_workers`
- `reserve_wave_budget`
- `prepare_replan`
- `controlled_finalize`

恢复节点通过官方 stream writer 发送安全 `custom` 事件：

```json
{
  "type": "recovery_transition",
  "from": "worker_failed",
  "to": "replan",
  "reason_code": "worker_operational_exhausted",
  "plan_generation": 2
}
```

事件不得包含参数、结果正文、异常详情或用户内容。planner/worker 位于父图子图内，SDK 消费者必须同时选择所需
`messages/updates/custom` mode 并启用 `stream_subgraphs=True`。Studio Graph UI 不作为 custom payload 渲染契约。

## 14. 安全与幂等

- frozen success 禁止重放，尤其是 generate/write/dangerous Tool 的结果。
- HITL 决策与 Tool 业务幂等仍由现有原生 middleware 和具体 Tool/API 拥有。
- recovery context 不得扩大身份、tenant、Tool catalog、Skill/reference grant 或网络权限。
- failed worker 的原始异常不进入模型；仅使用静态 code。
- retry/replan 后恢复必须从 checkpoint state 重算，不维护旁路 ready queue。
- 并行 worker outcome reducer 必须对重复 delivery 幂等、对冲突 fail closed。

## 15. 验证策略

实现阶段先在独立 `tests/tdd/<feature>/` 中建立 RED/GREEN，是否晋升永久 core invariant 按 `tests/README.md` 和
`assistant-agent-development-testing` skill 判断。

最低覆盖：

1. planner 第 9 次 Tool 被阻止后，前 8 次成功 Tool evidence 仍进入 state，并由新 generation 复用。
2. planner budget exhaustion 不触发 LangGraph node retry，不产生第二次相同 planner attempt。
3. 一个并行 worker 成功、另一个 operational retry 耗尽时，成功结果冻结且 replan 后不重放。
4. 新计划以唯一 node ID 替换失败节点，并完成原 deliverable。
5. business failure 直接 replan，不做相同输入 retry。
6. 并行 wave reservation 不突破全图 Tool/model/node attempt ceiling。
7. replan 上限耗尽后进入 controlled finalizer，返回标准 `AIMessage`。
8. Permission、schema/type/assertion、HTTP 非准入 4xx、interrupt 和 cancel 不被 recovery 转换。
9. checkpoint resume 不重复成功 Tool、worker 或 recovery transition。
10. reducer 对相同 execution ID 的相同值幂等，对不同值 fail closed。
11. `messages/updates/custom` 在 `stream_subgraphs=True` 时带正确 namespace 和稳定 recovery code。
12. 使用本次真实 trace 的脱敏离线回归：预算耗尽进入 replan，不再演变为盲目 Provider 二次调用。

实现后按 authority owner 复核并同步当前架构文档；若修改 authority，运行文档 authority validator。默认测试与所有
回归保持 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得为此设计自动调用真实 Provider。

## 16. 实施边界

预计涉及：

- `src/assistant_agent/native_agent/models.py`
- `src/assistant_agent/native_agent/state.py`
- `src/assistant_agent/native_agent/fast_agent.py`
- `src/assistant_agent/native_agent/planning_phase.py`
- `src/assistant_agent/native_agent/planning_graph.py`
- 对应临时 TDD 测试与必要的 core invariant
- `docs/runtime-event-stream-architecture.md` 及经 authority manifest 匹配的相邻 owner

实施必须保持共享 FastAgent、原生 ToolNode/ToolRuntime、原生 `Send` 并行、原生 checkpoint/interrupt 和现有权限
投影，不引入新的 Agent Runtime、关键词路由或不可序列化 Graph state。
