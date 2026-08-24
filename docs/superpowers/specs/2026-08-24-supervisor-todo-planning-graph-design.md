# Supervisor 驱动的 Todo Planning Graph 设计

状态：待用户复核

日期：2026-08-24

## 1. 文档定位与替代关系

本文定义 `assistant_agent` planning 模式下一代编排语义，重点解决以下问题：

- `Supervisor`、`Planner`、`Scheduler`、`Worker` 等角色边界含混；
- Planner 被实现成独立且过强的第二套 Agent，承担了不必要的协议和恢复责任；
- 严格 upfront DAG、generation、replacement ledger 和 admission correction loop 使正常恢复变脆；
- Worker 的标准 Tool 调用、结果消息、checkpoint 恢复和 Studio 可视化没有形成一套简单契约；
- Tool 并行风暴既缺少通用 harness 约束，也缺少 Skill 级具体流程约束。

本文在 planning 编排、恢复和可视化范围内，拟替代以下历史设计：

- `2026-08-20-native-high-agency-planner-design.md`
- `2026-08-21-planning-recovery-routing-design.md`

历史文档继续保留为设计记录。本文在用户批准并完成实现前，不改变 `docs/*.md` 当前 authority，也不表示现有生产实现已经迁移。

## 2. 背景与设计判断

当前 planning graph 把计划建模为严格的模型输出协议：Planner 先生成完整 DAG，admission 再校验 generation、节点 ID、替换关系、授权和预算，Scheduler 按 wave 执行，失败后 Planner 生成 recovery plan。该方案可验证，但把大量运行时事实暴露给模型，也使一次普通 Tool 限流可能升级成多轮 plan admission 失败。

Deep Agents 已验证了更简单的 plan-work 组织方式：一个 Supervisor 通过 todo 维护工作，通过标准 `task` Tool 调用隔离的 subagent，并从对应 `ToolMessage` 获取结果。它仍运行在 LangGraph 上，不是另一套 Runtime。

本项目不应照搬“把所有执行隐藏在一个 task Tool 内”的实现，因为我们还要求：

- Worker 分支能够继承 LangGraph checkpoint 和 pending writes 恢复语义；
- Studio 能看到显式 worker 子图、模型调用和 Tool 调用；
- planning 全局预算在并行派发前能够统一预留和结算；
- Skill 授权、Tool allowlist 和非 read Tool HITL 有明确的确定性边界。

因此选择混合方案：

> 使用 Deep Agents 风格的 Supervisor + todo + task 语义；使用显式 LangGraph Scheduler 和 Worker Agent 子图承载执行、恢复与观测。

这里的 subagent 与 subgraph 不是互斥概念：`Worker` 是语义上的 subagent，同时由编译后的 Agent graph 作为运行载体。

## 3. 术语

### 3.1 规范定义

| 名词 | 定义 | 拥有的职责 | 明确不拥有的职责 |
| --- | --- | --- | --- |
| **Supervisor** | planning 模式中唯一由模型驱动的协调者和用户目标所有者 | 加载 Skill、维护 todo、选择 ready task、消费 Worker 结果、决定继续或总结 | 不直接执行业务 Tool；不预留预算；不实现 checkpoint；不伪造 Worker 结果 |
| **Planner** | Supervisor 的一种行为或阶段，而不是独立 Agent、独立节点或第二个大脑 | 创建、修订 todo；表达依赖；识别下一步工作 | 不拥有单独状态机；不产生运行时 execution ID；不直接调 Worker 或业务 Tool |
| **Planning Scheduler** | planning graph 内无 LLM 的确定性调度组件 | 校验 task 调用、校验依赖与授权、预留预算、派发 Worker、join 和结算 | 不理解或改写用户目标；不创造任务；不 replan；不生成最终答案 |
| **Worker** | 执行单个 task 的语义 subagent，由 `create_agent` 编译子图承载 | 在窄上下文和 Tool allowlist 内完成工作，返回结构化结果 | 不加载新 Skill；不扩大授权；不修改 todo DAG；不决定整个 run 是否结束 |
| **Todo** | checkpointed 的业务工作项 | 描述目标、状态、依赖和所需能力 | 不是 LangGraph node，也不是一次具体执行 attempt |
| **Plan** | 当前 todo 集合及其 `depends_on` 关系 | 表达可动态修订的业务 Task DAG | 不是一次性冻结的完整执行协议 |
| **Task call** | Supervisor 对 ready todo 发出的标准 `task` ToolCall | 请求执行既有 todo | 不携带任意新目标，不越过 todo 修改流程 |
| **Execution / attempt** | Graph 为一次实际派发生成的运行时事实 | 关联预算、trace、重试和结果 | 不由 Supervisor 或 todo 指定 |
| **Finalization** | Supervisor 的无 Tool 总结阶段 | 基于已完成结果和未完成项生成标准 `AIMessage` | 不是独立的 Finalizer Agent 或第三个大脑 |

### 3.2 命名约束

- 生产 graph 中表达模型协调节点时统一使用 `supervisor`，不再用 `planner` 暗示独立角色。
- `planning` 继续表示 execution mode；“规划阶段”可以称为 Supervisor planning behavior。
- 本文的 `Planning Scheduler` 与 Agent Server 内负责 run queue 的 queue scheduler 完全不同。代码和文档不得只写含混的 `scheduler`。
- `Worker` 大写时指 planning role；小写 `worker` 可用作 graph node 名。
- 不再使用 Planner-facing 的 `plan_generation`、`replaces_node_ids`、replacement claim ledger 或历史 execution ID。

## 4. 设计目标与非目标

### 4.1 目标

1. 只保留一个模型协调角色，消除 Planner 与 Finalizer 的重复心智模型。
2. 让计划像 Deep Agents 一样可动态维护，而不是要求模型一次生成完整且严格的执行协议。
3. 使用标准 ToolCall / ToolMessage 表达 task 委派与结果，写入标准 `messages` state。
4. 使用 LangGraph 原生 checkpoint、pending writes、interrupt/resume 和 subgraph stream。
5. 保留 Skill 渐进暴露与冻结、Worker Tool allowlist、非 read Tool HITL、planning 全局预算。
6. 用通用 harness 规则限制 Worker 同名 Tool 并行风暴，并用 Skill 流程约束领域策略。
7. 让 Studio 能同时看见 Supervisor、Planning Scheduler、Worker 子图、业务 Tool 和最终总结。

### 4.2 非目标

- 不自建 checkpoint store、run manager、消息协议或 UI 专用事件总线。
- 不把 planning 退化成一个不可观测的单节点 task Tool 黑盒。
- 不把 todo 变成项目管理系统；它只服务当前 thread/run 的模型工作记忆和调度。
- 不用关键词、正则或入口话术推断用户意图或选择 Tool。
- 不通过盲目提高 Tool 上限解决低质量调用、限流或 fan-out。
- 不保证 replay 能撤销已发生的外部副作用。

## 5. 方案选择

### 5.1 备选方案

| 方案 | 优点 | 主要问题 | 结论 |
| --- | --- | --- | --- |
| 严格 Planner DAG + admission + recovery plan | 静态契约强、易做形式校验 | 模型承担运行时 ID 和替换协议；错误恢复过度定制；纠错循环脆弱 | 不再采用 |
| 直接使用单个 `task` Tool 包装所有 subagent | 接近 Deep Agents 默认体验；实现紧凑 | 父图难以显式观察 Worker state；预算预留和 Studio 拓扑较弱 | 不作为生产主形态 |
| Supervisor todo/task + 显式 Scheduler/Worker 子图 | 动态规划、标准消息、原生恢复、显式观测、安全边界兼得 | 比直接 task Tool 多一层确定性编排 | **采用** |

### 5.2 原生优先边界

采用 LangChain/LangGraph 官方组件能解决的部分：

- `create_agent`、`BaseTool`、`ToolNode`、`ToolRuntime`；
- `TodoListMiddleware` 的 todo/tool-message 思路；
- `ToolRetryMiddleware`、`HumanInTheLoopMiddleware`、模型/Tool call limit middleware；
- `StateGraph`、`Send`、reducer、checkpointer、interrupt/resume 和原生 stream modes。

项目只保留官方组件没有覆盖的窄策略：

- Skill/Reference/Tool 授权 envelope；
- task 与 todo 的一致性校验；
- 并行 wave 的全局预算预留和结算；
- Worker 单次模型响应的同名 Tool 并行上限；
- 面向产品安全的 custom lifecycle 事件脱敏。

## 6. 总体 Graph 结构

```text
START
  |
  v
supervisor <-------------------------------+
  |                                        |
  | load_skill / load_reference /          |
  | write_todos                            |
  v                                        |
supervisor_controls -----------------------+
  |
  | task ToolCalls
  v
authorize_and_reserve
  |
  | Send(worker, task execution) * N
  +------------+------------+--------------+
  |            |            |
  v            v            v
worker       worker       worker
(Agent       (Agent       (Agent
subgraph)    subgraph)    subgraph)
  |            |            |
  +------------+------------+
               |
               v
        join_and_reconcile
               |
               | Worker ToolMessages + results
               v
          supervisor
          /        \
   more work      finalize
      |              |
      +--------------+--> END
```

关键性质：

- `supervisor` 是唯一模型协调节点，可多次进入；Planner 不是另一个节点。
- `supervisor_controls` 只处理控制 Tool，不执行业务 Tool。
- `authorize_and_reserve`、`join_and_reconcile` 都是确定性节点，合称 Planning Scheduler。
- `worker` 是显式编译 Agent 子图，可通过 `Send` 并行派发。
- `finalize` 是 Supervisor 的 tool-free phase，可以是显式节点以改善 Studio 可见性，但使用同一 Supervisor 模型和同一目标上下文。

## 7. Supervisor 契约

### 7.1 唯一职责中心

Supervisor 持有当前用户目标的完整语义，负责回答三个问题：

1. 还需要了解哪个 Skill 或 reference？
2. 当前 todo 应如何创建或修订，哪些 task 已 ready？
3. 已有结果是否足以回答；若不足，下一步是什么？

Supervisor 不做以下工作：

- 不直接看到或调用业务 Tool schema；
- 不自行执行旅行、购物、地图、搜索等业务 Tool；
- 不生成预算 reservation、execution ID 或 attempt number；
- 不把 operational exception 改写成虚构的业务结果；
- 不在 finalization 阶段继续调用 Tool。

### 7.2 Supervisor 可调用能力

Supervisor 的可执行控制能力限定为：

- `load_skill`
- `load_skill_reference`
- `write_todos`
- `task`

这些是标准 ToolCall 形态，但由确定性控制节点或 Planning Scheduler 处理。Supervisor 只能看到 Skill 激活后投影出的**能力目录**，例如 Tool 名称、用途、读写类别和约束；不能获得业务 Tool 的可执行参数 schema。

单个 Supervisor AIMessage 必须属于以下一种控制意图：

- Skill/reference materialization；
- `write_todos` 更新计划；
- 一个或多个并行 `task` 调用；
- 无 Tool 的 finalization。

同一消息不得混合计划修改与 task 派发，也不得混合授权 materialization 与 task 派发。控制 middleware 对混合调用生成配对 error ToolMessages，避免一边改变调度快照、一边按旧快照派发。

### 7.3 开局是否必须使用 todo

planning 模式要求在首次 Worker 派发前存在至少一个有效 todo。推荐顺序为：

1. Supervisor 读取用户目标和可信上下文；
2. 若任务依赖领域知识，先 `load_skill`，必要时加载最小 reference；
3. `write_todos` 建立初始计划；
4. 对 ready todo 发出一个或多个 `task` ToolCall；
5. 消费结果后动态更新 todo 或进入 finalization。

这不是要求第一条模型消息机械调用 `write_todos`。Skill 决定流程和能力边界时，Skill 应先于权威 todo；但没有 todo 就不能派发 Worker。

### 7.4 动态规划而非 upfront 完整协议

Supervisor 可以在每个 wave 后：

- 标记已完成工作；
- 修订尚未执行的 todo 内容或依赖；
- 新增由结果自然衍生的 todo；
- 删除不再需要的 pending todo；
- 把缺少证据的工作标为 blocked；
- 在信息已足够时停止剩余非必要工作并总结。

已完成 todo 及其结果被冻结，Supervisor 不能重写或重新派发。正在执行的 todo 不能被同时修改。除此之外，不要求模型维护 generation 或 replacement chain。

## 8. Todo 与动态 Task DAG

### 8.1 最小模型

```python
class PlanningTodo(BaseModel):
    todo_id: str
    objective: str
    status: Literal["pending", "in_progress", "completed", "blocked", "cancelled"]
    depends_on: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    expected_output: str | None = None
```

`required_capabilities` 引用已激活 Skill 投影出的受控 capability ID，不承载 Tool 参数。实际 Tool allowlist 由 Planning Scheduler 确定。

### 8.2 DAG 的准确含义

todo 可以理解为业务 DAG，但需要区分四种结构：

1. 编译后的 agent graph 是包含 `supervisor -> controls/task -> supervisor` 的循环图；
2. 当前 todos 与 `depends_on` 形成可动态修改的业务 DAG；
3. 某次 run 展开的具体执行和消息因果关系形成 execution DAG；
4. checkpoint/replay 形成运行历史分支 DAG。

因此“Supervisor 维护 todos”可以表达 DAG，但 todo 本身不是 LangGraph 静态 topology；“模型动态调用 task Tool”会展开执行 DAG，但每次 ToolCall 本身也不等于一张完整 DAG。

### 8.3 Todo 不变量

`write_todos` 只做最小确定性校验：

- `todo_id` 在当前计划内唯一；
- `depends_on` 只引用当前存在的 todo，且不能形成环；
- 只有所有依赖均 completed 的 pending todo 才是 ready；
- completed todo 和已冻结结果不可修改或删除；
- in-progress todo 不可修改、删除或重复派发；
- 首次冻结前，新增或修改 todo 的 capability 必须位于已激活 Skill 的候选授权目录；冻结后则必须位于 authorization envelope；
- todo 数量和内容长度受可信配置限制。

校验失败返回标准 error `ToolMessage`，让同一个 Supervisor 在普通 agent loop 中修正；不进入独立的 proposal admission revision 状态机。

### 8.4 状态迁移

```text
pending --task accepted--> in_progress
in_progress --succeeded--> completed
in_progress --partial/semantic failure--> pending | blocked
pending --no longer needed--> cancelled
blocked --Supervisor revision--> pending | cancelled
```

具体 Worker attempt 的 succeeded/partial/failed 是运行时结果，不等于必须把 todo 永久终结为 failed。Supervisor 可以根据结果修订 pending/blocked todo，但不能覆盖已经 completed 的事实。

## 9. Task ToolCall / ToolMessage 协议

### 9.1 Task call

Supervisor 只对既有 ready todo 发出：

```json
{
  "name": "task",
  "args": {"todo_id": "route_options"},
  "id": "model_tool_call_id"
}
```

`task` 不接受任意新 instruction、Tool allowlist、Skill grant、预算或 execution ID。目标、依赖、预期输出和 capability 均从 checkpointed todo 读取，避免 task call 绕过 `write_todos`。

同一 Supervisor 消息可以包含多个不同 ready todo 的 `task` ToolCall，从而表达一个并行 wave。

### 9.2 Worker 结果

```python
class WorkerResult(BaseModel):
    todo_id: str
    status: Literal["succeeded", "partial", "blocked"]
    summary: str
    facts: tuple[StructuredFact, ...] = ()
    missing: tuple[str, ...] = ()
    retry_hint: str | None = None
```

结果不得包含原始异常、secret、宿主路径或未受控 Provider payload。事实应保留来源/证据标识，供最终总结判断。

### 9.3 ToolMessage 必须进入 state

是的，父 Supervisor 的 task 结果必须作为标准 `ToolMessage` 写入 `messages`，并使用原始 task ToolCall 的 `tool_call_id`：

- Supervisor 的 AIMessage 发出 task ToolCalls；
- Worker 子图内部拥有自己的 Human/AI/ToolMessage 序列；
- Worker 完成后先把结构化 `WorkerResult` 写入并行结果 channel；
- `join_and_reconcile` 按原始 ToolCall 顺序确定性生成父级 task `ToolMessage`；
- Supervisor 下一轮同时读取 task ToolMessages 和结构化结果摘要。

由 join 统一写父级 ToolMessages，而不是各并行 Worker 直接竞争 `messages` reducer，可保证 ToolCall/ToolMessage 配对和顺序稳定。Worker 内部的业务 ToolMessages 留在 Worker subgraph/checkpoint/trace 中，不把完整正文无界复制到父级；父级只提升受控的最终报告。

## 10. Planning Scheduler 契约

### 10.1 authorize_and_reserve

该确定性节点逐个处理 task ToolCall：

1. ToolCall 名称必须是 `task`，参数只包含 `todo_id`；
2. todo 必须存在、为 pending 且依赖已完成；
3. 同一 wave 不得重复派发同一 todo；
4. 所需 capability 必须在冻结授权 envelope 内；
5. Worker Tool allowlist 必须是 todo capability、授权 envelope 和实际 inventory 的交集；
6. 全局剩余 model/tool/node/replan allowance 必须足够完成保守预留；
7. 为每次执行生成内部 `execution_id`，把 todo 标为 in-progress；
8. 使用 `Send("worker", WorkerInput(...))` 派发通过校验的任务。

无效 task call 返回对应 error ToolMessage，不创建 Worker、不消费实际 Tool usage。若一条消息中部分 task 合法、部分非法，合法任务可以执行；非法任务以标准错误关闭对应 ToolCall，Supervisor 下一轮修正。

### 10.2 join_and_reconcile

join 节点：

- 按 task ToolCall 原始顺序归并结果，而不依赖并行完成顺序；
- 结算 reservation 与实际 usage；
- 冻结 succeeded 结果并把 todo 置为 completed；
- 将 partial/blocked 结果映射为 pending 或 blocked；
- 生成与每个 task ToolCall 配对的父级 ToolMessage；
- 清理已结算 reservation；
- 回到 Supervisor。

### 10.3 Scheduler 不得做的事

- 不根据自然语言创造 todo 或修改 objective；
- 不自行选择新 Skill、reference 或 capability；
- 不把失败工作替换成新工作；
- 不调用模型决定是否重试；
- 不写最终自然语言答案。

## 11. Worker Agent 子图

### 11.1 构建方式

Worker 复用 fast 模式的 `create_agent` 基座和标准 Tool 执行链，但使用独立的 scoped input/context：

- 一个明确 todo objective；
- 直接依赖的 frozen WorkerResult 摘要；
- 当前 task 所需的 Skill 指导和已授权 reference；
- 确定性投影出的 Tool allowlist；
- 该 task 的局部预算和只读可信事实；
- 父 thread/run/checkpoint 资源的原生子图继承。

Worker 不接收完整父 messages、其他无关 todo、未授权 Skill 内容或可扩大授权的控制 Tool。

### 11.2 Tool 权限

Worker 可执行 Tool 集合为：

```text
requested capabilities
  INTERSECT frozen authorization envelope
  INTERSECT current trusted Tool inventory
  INTERSECT execution-mode safety policy
```

Worker 不能调用 `load_skill`、`write_todos` 或 `task`。允许它读取已授权且按需 materialize 的 reference，但不能新增 reference grant。

### 11.3 非 read Tool HITL

- read Tool 使用官方有界 retry middleware；
- planning Worker 的非 read Tool 使用官方 `HumanInTheLoopMiddleware`；
- interrupt 与 resume 由 Agent Server/LangGraph 原生处理；
- resume 后已完成的并行 Worker 和 Tool 不重放；
- write/dangerous Tool 的幂等性仍归具体 Tool 或业务 API，不能由 checkpoint 替代。

## 12. Skill 冻结与渐进暴露

### 12.1 两个概念必须分开

- **Grant**：允许本 run 使用哪些 Skill、reference ID 和 Tool/capability ID；
- **Materialization**：何时把已授权 reference 的正文实际加载进模型上下文。

首次接受 task 派发时冻结 authorization envelope：

- 已激活 Skill ID；
- 从 Skill manifest 选择的 reference grant ID；
- 当前计划允许的 capability/Tool ID；
- 安全类别与 entry/env 结构化约束。

冻结后不得新增 grant，只能保留或收窄。但 Supervisor 和 Worker 可以在已有 reference grant 内渐进 materialize 内容，因此“冻结”不等于开局把所有 Skill reference 全塞进上下文。

### 12.2 授权扩展

运行中如果用户目标真正变化并需要新能力，不允许 Supervisor 静默扩大 envelope。应由明确的新用户输入触发新的 planning authorization cycle；若 checkpoint schema 无法安全表达同 thread 的 re-entry，则启动新 run/thread。具体入口产品行为留给实施阶段确认，但不得把 replan 当成扩权通道。

## 13. 全局预算与 Tool 调用限制

### 13.1 planning 全局预算

全局预算来自受信 production composition，至少跟踪：

- model calls；
- 实际执行的 Tool calls；
- graph node attempts；
- Supervisor replan/wave 次数；
- 可选 token/time allowance。

Supervisor、Worker、用户输入和 Skill 文本都不能提高预算。Planning Scheduler 在并行派发前进行保守 reservation，join 后按实际 usage 结算。必须为正常 finalization 或 controlled fallback 保留终态槽位。

预算耗尽是可解释的受控终止条件，不应伪装成任意节点的 raw exception，也不应通过不断生成 recovery plan 消耗更多预算。

### 13.2 Worker 同名 Tool 并行上限

所有 planning Worker 使用通用 after-model harness 规则：

> 单次模型响应中，同一个业务 Tool 最多并行执行 3 次。

规则行为：

- 按 Tool name 对当前 AIMessage 的待执行 ToolCalls 分组；
- 每组最多放行前 3 个；
- 超出的调用不执行，并立即生成配对的标准 error ToolMessage；
- 不同 Tool name 仍可并行；
- 被阻止调用不计入实际 Tool usage，但可记入单独 attempted-call telemetry；
- 模型可根据错误收缩候选，而不是让整个 atomic batch 失败；
- 该规则不读取用户文本、Tool 参数或领域关键词。

这与 run/thread 级 `ToolCallLimitMiddleware` 互补：官方 middleware 负责总量，本规则负责单次模型响应的同名 fan-out。

### 13.3 为什么不能只提高 Tool 上限

提高 Worker Tool 上限只能推迟失败，无法解决：

- 对大量候选做无必要的同名调用；
- Provider rate limit；
- atomic batch 因少数失败而整体回退；
- 重试时遗忘已成功结果；
- Skill 没有给出“何时证据已足够”的停止条件。

因此 Tool 总量仍有界；达到边界时 Worker 应尽量返回 partial/blocked 结构化结果，让 Supervisor 使用已有事实总结或修订 todo。

## 14. Skill 流程必须具体：旅行/地图示例

旅行 Skill 的 POI/路线流程应至少包含以下规范，作为首个落地回归场景：

1. 文本搜索或附近搜索通常只执行一次；
2. 先用名称、类型、行政区和地址缩小候选；
3. 最多保留 3 个最终候选；
4. 只有候选缺少坐标且下游路线计算确实需要时才调用 geocode；
5. 同一批 geocode 最多 3 个；
6. 遇到 rate limit 时保留已成功结果，不立即 fan-out 重试全部失败项；适用时由 adapter 做有界退避重试；
7. 证据足够时立即完成；不足时明确返回 unknown/pending，而不是补齐所有候选；
8. 明确禁止对文本搜索返回的全部候选逐个 geocode。

通用 harness 上限阻止灾难性 fan-out，Skill 流程提升模型决策质量；两者缺一不可。

## 15. Checkpoint、恢复与失败语义

### 15.1 Operational failure：交给 LangGraph

以下故障原则上保留为异常并传播到 LangGraph/Agent Server：

- model/provider timeout、connection error；
- retry middleware 耗尽后的 transient read Tool 故障；
- `NodeCancelled`、`GraphBubbleUp` 等控制流异常；
- 进程中断或 run cancel。

恢复使用原生 checkpoint 和 pending writes：同一 superstep 中已经成功完成的并行分支写入 pending writes；恢复失败任务时，不重放已成功分支。项目不把每个 operational exception 包装成自定义 WorkerOutcome，也不额外运行 recovery router。

Supervisor 能从 checkpointed completed todos、frozen WorkerResults 和 task ToolMessages 看到成功分支，再修订尚未完成的 todo。

### 15.2 Semantic failure：返回结构化结果

以下情况由 Worker 返回 `partial` 或 `blocked`：

- 找不到足够业务证据；
- 输入存在不可消除的歧义；
- 局部 Tool/model 预算耗尽但已有部分有效结果；
- Tool 明确返回可解释的领域失败；
- 继续调用已无合理收益。

这类结果进入 checkpoint 和父级 ToolMessage，由 Supervisor 决定修改 todo、换路径或基于部分结果总结。

### 15.3 Deterministic control error

todo/task 参数错误、依赖未满足、授权越界、同一 todo 重复派发等确定性错误，通过配对 error ToolMessage 返回 Supervisor。它们不应终止整个 run，也不进入独立的最多两次 proposal revision 协议。

### 15.4 Replay、fork 与副作用

- resume：从最新失败 checkpoint 继续；
- replay/fork：从指定历史 checkpoint 创建新的执行分支；
- rollback/cancel：遵循 Agent Server 原生语义。

这些能力不撤销已经发生的外部副作用。read Tool 可安全重试；write/dangerous Tool 必须同时依赖 HITL、Tool/API 幂等键和业务补偿策略。

## 16. Finalization 与受控终态

### 16.1 正常 finalization

当 Supervisor 判断信息足够、全部必要 todo completed，或剩余 todo 不值得继续时，进入 tool-free finalization：

- 使用同一 Supervisor 模型；
- 只读取用户目标、completed/blocked todo、冻结结果和缺失项；
- 不暴露任何 Tool；
- 输出标准 `AIMessage`；
- 清楚区分已确认事实、推断、未完成项和限制。

它可以作为显式 `finalize` graph node 提升 Studio 可见性，但不是独立 Finalizer Agent。

### 16.2 Controlled fallback

若全局预算不足以再调用模型，但仍需要关闭 run，确定性 fallback 生成标准 `AIMessage`，至少列出：

- 已完成 todo 及简短结果；
- blocked/pending todo；
- 停止原因；
- 用户可采取的下一步。

模型/provider 的 operational failure 不自动降级为 fallback，应保留 checkpoint 以便原生 resume。fallback 只处理明确、可判定的预算或策略终态。

## 17. State 与 reducer

planning state 至少包含：

```python
class SupervisorPlanningState(AgentState):
    todos: tuple[PlanningTodo, ...]
    authorization_envelope: AuthorizationEnvelope | None
    pending_task_calls: tuple[PendingTaskCall, ...]
    worker_results: tuple[WorkerResultRecord, ...]
    frozen_results: tuple[WorkerResultRecord, ...]
    budget_usage: PlanningBudgetUsage
    wave_reservations: tuple[WaveReservation, ...]
    trusted_runtime_facts: dict[str, JSONValue]
```

约束：

- `messages` 继续使用官方 `add_messages` reducer；
- completed result 使用按 `todo_id` 冻结且幂等的 reducer；
- 并行 Worker 结果按内部 `execution_id` 聚合，join 后确定性排序；
- ready 集合从 todos、依赖和结果重算，不维护平行 shadow state；
- reservation 必须 checkpointed，reconcile 幂等；
- state 只保存 JSON-safe、受控、可反序列化值；
- 不保存原始异常、完整 Tool 参数、secret 或无界 Provider payload。

## 18. Agent Server 与 Studio 原生可视化

### 18.1 标准 stream

planning 消费者应使用 Agent Server/LangGraph 原生模式：

- `messages`：Supervisor/Worker 模型 token 与标准消息；
- `updates`：节点和 state 更新；
- `custom`：脱敏的 lifecycle 事件；
- `subgraphs=True`：Worker Agent 子图 token、Tool 和状态路径；
- trace/Studio：查看静态 topology 与具体 run 展开。

显式 Worker 节点使 Studio 至少能看到：

```text
supervisor
authorize_and_reserve
worker/*
join_and_reconcile
finalize
```

标准 task ToolCalls 和父级 ToolMessages 让用户知道“委派了什么、哪个 task 已返回”；Worker subgraph trace 让用户知道“正在调用哪个模型/Tool”。

### 18.2 Custom 事件

custom 只补充安全生命周期，不复制标准消息正文：

- `planning_todo_updated`
- `planning_task_started`
- `planning_task_completed`
- `planning_task_blocked`
- `planning_budget_updated`

字段限定为 todo/execution ID、Tool name、状态、计数和受控 reason code；不得包含 Tool 参数、ToolMessage 正文、artifact、异常正文或 Skill 私有内容。

### 18.3 Studio 与 Chat 的边界

Graph 本身支持原生流式事件；不是必须切到 Chat 才能看到 planning 执行。Studio 是否把某种 stream mode 渲染成最佳交互体验取决于 Studio 版本和客户端选择，但 Runtime 必须产出标准 stream，而不能为 UI 再造协议。

Chat 入口继续通过结构化 `execution_mode=fast|planning|coding` 选择模式；固定 planning assistant preset 只是同一 graph 的 context preset，不是另一套 Runtime。

## 19. Graph 版本与迁移

本设计改变 planning topology、state schema、ToolCall/ToolMessage 协议和恢复语义，不能让旧 `assistant-native-v2` pending checkpoint 直接进入新图。

实施时应：

- 注册新的版本化 graph ID，建议 `assistant-native-v3`；
- 同步创建 planning preset assistant，建议 `assistant-native-v3-planning`；
- 默认 assistant 和 planning preset 都绑定 v3 graph metadata；
- 对 thread/run 执行 graph ID guard；
- v2 pending/interrupt run 先 drain 或 cancel；
- v2 历史 checkpoint 只读，不自动 migration、resume 或 replay 到 v3；
- 更新媒体确定性 thread seed，避免 v2 thread 被 v3 复用。

若实施评估证明 state/checkpoint 完全兼容，仍需提供证据并经过专项复核；默认按不兼容升级处理。

## 20. 从当前实现迁移

### 20.1 删除或降级的概念

- 删除独立 Planner Agent/节点命名，改为 Supervisor；
- 删除 Planner-facing `NativePlanProposal` 严格 upfront plan 协议；
- 删除 `plan_generation`、`replaces_node_ids`、superseded ID 与 replacement claim ledger；
- 删除 bounded proposal admission correction loop；
- 删除把 operational Worker failure 统一改写为 recovery outcome 的路由；
- 删除独立 Finalizer Agent 语义，保留同一 Supervisor 的 tool-free phase；
- 不再把提高单 Worker Tool 上限作为主要恢复手段。

### 20.2 保留并重构的能力

- 复用 fast `create_agent` 基座；
- Skill loading 和 reference materialization；
- 冻结 authorization envelope；
- Worker Tool allowlist；
- non-read Tool HITL；
- phase/global planning budget；
- `Send` 并行 Worker；
- 标准 messages、custom lifecycle、Agent Server checkpoint/stream；
- controlled terminal summary。

## 21. 验证策略

实施遵循 mock/local/offline，除非用户另行明确授权，不调用真实 Provider。测试范围在实施计划中按 `tests/README.md` 和项目 testing skill 决定。

至少验证：

### 21.1 术语与角色

- 只有 Supervisor 是模型协调者；
- Planner 不再作为 graph node/class runtime role；
- Planning Scheduler 无 LLM，不能产生 todo 或自然语言答案；
- finalization 不暴露 Tool。

### 21.2 Todo/task

- 无 todo 不得 task；
- depends_on 未完成不得派发；
- 多个 ready task 可并行；
- completed todo/result 不可改写或重放；
- 非法 todo/task 返回标准 error ToolMessage 后可由 Supervisor 修正；
- 父 messages 中 task ToolCall 与 ToolMessage 一一配对。

### 21.3 Checkpoint/resume

- 两个并行 Worker 一成一败时，resume 只重试失败分支；
- Supervisor 能读取已成功结果并修订剩余 todo；
- interrupt_after join/finalize 后 resume 不双计预算、不重放 ToolMessage；
- control-flow exception 沿异常链保持原生语义；
- v2 thread/checkpoint 不能进入 v3 run。

### 21.4 Tool 与 Skill

- 同一 AIMessage 第 4 个同名 ToolCall 被 error ToolMessage 关闭且不执行；
- 三个以内同名 ToolCall 正常并行，不同 Tool name 不受该组上限影响；
- blocked call 不计实际 Tool usage；
- Worker 不能加载 Skill 或调用 envelope 外 Tool；
- 非 read Tool interrupt/resume 使用原生 HITL；
- 旅行 Skill 不会对 20 个文本候选批量 geocode，且能保留部分成功结果。

### 21.5 Stream/Studio

- `messages` 可见 Supervisor 和启用 subgraph 后的 Worker token；
- `updates` 可见 authorize/worker/join/finalize 节点；
- `custom` 事件完整且不泄漏参数/结果；
- 最终一定有标准 AIMessage，或 run 保持可 resume 的原生 operational error 状态。

## 22. 验收标准

本设计完成实施后，应满足：

1. 团队对 Supervisor、Planner、Planning Scheduler、Worker 的使用不存在两套定义。
2. planning 模式只有一个模型协调角色，Planner 是行为而不是组件。
3. Supervisor 以 todo 开始可执行规划，以 task ToolCall 派发 ready 工作。
4. Worker 是显式 Agent subgraph，业务 Tool 只在 Worker 内执行。
5. 父 `messages` 保存标准 task ToolCall/ToolMessage；Worker 细节保留在 subgraph trace。
6. operational failure 使用 LangGraph 原生 checkpoint 恢复，成功并行分支不重放。
7. Skill grant 冻结但 reference 内容可在 grant 内渐进 materialize。
8. Worker Tool allowlist、非 read HITL 和 planning 全局预算保持有效。
9. 单次模型响应的同名 Tool 并行数不超过 3。
10. 旅行 Skill 有具体的候选收缩、geocode 和 rate-limit 流程。
11. Studio/Agent Server 可通过原生 messages/updates/custom/subgraphs 看见 planning 生命周期。
12. 正常完成或预算受控终止时，用户能得到包含结论、已完成项和缺失项的标准 AIMessage。

## 23. 最终决策摘要

- **谁负责全局思考？** Supervisor。
- **Planner 是谁？** Supervisor 在创建和修订 todo 时的行为，不是独立 Agent。
- **Scheduler 是谁？** 无 LLM 的 Planning Scheduler，只做校验、预算、派发、join 和结算。
- **todo 是不是 DAG？** 带 `depends_on` 的当前 todo 集合是动态业务 DAG；它不是静态 LangGraph topology。
- **task ToolCall 是不是 DAG？** 多次 task 调用展开成 execution DAG，但单次 ToolCall 只是一次派发请求。
- **ToolMessage 是否写入 state？** 写；父级保存 task 最终 ToolMessage，Worker 内部业务 ToolMessages 保存在子图。
- **Planner 开局能否用 todo？** 应该；若需要 Skill，先加载 Skill，再建立首份权威 todo，之后才可派发 Worker。
- **与 Deep Agents 的关系？** 借用 Supervisor/todo/task 的成熟语义，同时用显式 Worker graph 保留本项目需要的 checkpoint、预算、安全与 Studio 观测。

## 24. 参考资料

- [Deep Agents overview](https://docs.langchain.com/oss/python/deepagents/overview)
- [Deep Agents subagents](https://docs.langchain.com/oss/python/deepagents/subagents)
- [Deep Agents architecture](https://github.com/langchain-ai/deepagents/blob/main/libs/ARCHITECTURE.md)
- [LangChain TodoListMiddleware](https://reference.langchain.com/python/langchain/agents/middleware/todo/TodoListMiddleware)
- [LangChain agent middleware](https://reference.langchain.com/python/langchain/agents/middleware)
- [LangChain custom multi-agent workflows](https://docs.langchain.com/oss/python/langchain/multi-agent/custom-workflow)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
