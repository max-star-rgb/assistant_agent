# 原生高自由度 Planner 设计

## 1. 状态与目标

本文记录已确认的 planning 模式演进方案。目标是让 Planner 面对“明天想去安吉玩漂流”这类开放请求时，能够像一个能力完整的 Agent 一样主动判断是否需要天气、攻略、住宿、路线、美食和周边景点等信息，先进行必要探索，再生成可并行执行的 DAG，最终交付完整且有真实证据支撑的回答。

这里的“高自由度”不是新增一套自研 Agent Runtime，而是：

- Planner 与普通 fast agent 复用同一个原生 Agent 构建和工具装配基座；
- Planner 首轮看到与 fast agent 相同的 Tool 投影，并可真实调用这些 Tool；
- Planner 可通过 `load_skill` 获得领域方法和受治理 Tool；
- Planner 的系统提示词优先规定其角色：先理解、必要时探索，终态必须是结构化计划，而不是直接面向用户作答；
- 后续 DAG 调度、状态持久化、恢复、重试、interrupt 和流式事件均复用 LangGraph 原生能力。

## 2. 设计原则

### 2.1 原生优先

生产主链只使用现有 `AssistantRootGraph`、LangGraph `StateGraph`、`create_agent`、`ToolNode`、middleware、`Send`、checkpoint、interrupt/resume 和 Agent Server 生命周期能力。不得引入第二套 Runtime、队列、checkpoint adapter、repair ledger 或 scheduler 数据库。

### 2.2 同一基座，不同角色

Planner 不是 fast agent 的复制实现。两者共享 Agent factory、模型接入、Tool inventory、Tool exposure、Skill middleware、retry、HITL 和事件体系；差异集中在角色契约与终态：

| 维度 | fast agent | Planner |
| --- | --- | --- |
| 首轮 Tool 投影 | 当前请求可见的默认 Tool 与控制 Tool | 完全相同 |
| Skill | 可按需加载并扩展授权 | 可按需加载并扩展授权 |
| 主要目标 | 直接解决用户请求 | 探索后形成可执行计划 |
| 终态 | 用户可见回答 | `NativePlanProposal` |
| 后续执行 | 当前 Agent 自行完成 | admission 后交给 scheduler 与 workers |

Planner 的稳定系统提示词必须位于角色约束层，明确“规划优先”。动态 Skill 内容只能增强领域认知和 Tool 使用方法，不能覆盖以下终态契约：

1. 判断请求是窄任务还是开放任务；
2. 只进行能提高全局规划质量或被多个节点复用的前置探索；
3. 将剩余工作组织为结构化 DAG；
4. 不直接向用户输出最终答案；
5. 即使探索已经解决简单请求，也输出合法的零节点计划，由 finalizer 统一生成用户回答。

### 2.3 自由探索，受治理执行

Planner 可以真实调用业务 Tool，而不是只看到 Tool schema 或只能加载 Skill。其自由度仍受现有治理约束：

- 默认 Tool 来自受信静态装配和结构化上下文，不使用关键词、正则或手写意图路由预选；
- Skill-governed Tool 只有在 Skill 实际加载并形成有效 grant 后才可用；
- read Tool 使用原生 retry middleware；
- planning 模式中的非 read Tool 保留原生 HITL；
- Tool 参数校验、结果结构化、mock/real Provider 边界和幂等性仍由既有 adapter 与 Tool 负责；
- Planner 使用独立的调用次数、时间和证据体积预算，避免无限探索。

## 3. 总体架构

planning 分支采用以下原生 LangGraph 数据流：

```text
request
  -> planner_agent
  -> admit_plan
  -> scheduler
       -> Send(worker, node) ...
       -> join
       -> scheduler ...
  -> coverage_audit (可选条件边)
       -> planner_agent (需要 replan 时)
       -> finalizer
  -> response
```

各组件职责如下：

- `planner_agent`：复用 fast agent 基座，执行必要探索并产出结构化计划。
- `admit_plan`：确定性验证 DAG 结构、引用和授权，不判断业务领域是否“应该包含酒店”。
- `scheduler`：根据 checkpoint 中的计划和执行结果计算 ready nodes，并用原生 `Send` 发出一波并行任务。
- `worker`：复用同一个 fast agent，以受限任务上下文执行单个计划节点。
- `join`：归并 worker 结果，不维护自定义队列状态。
- `coverage_audit`：可选的模型节点或确定性策略边，用于决定完成、失败收束或重新进入 Planner；其状态仍保存在原生 graph state 中。
- `finalizer`：不调用 Tool，只基于用户请求、PlannerEvidence、计划和 worker 结果形成最终回答。

## 4. Planner Agent

### 4.1 构建方式

Planner 应通过共享的 `AssistantFastAgent`/Agent factory 构建，而不是直接对模型调用 `with_structured_output` 并绕过 Agent loop。Planner 配置包括：

- 与 fast agent 相同的初始 Tool inventory 和 exposure projection；
- planning 专属系统提示词；
- planning 上下文中的 Skill、retry、HITL 和 observability middleware；
- 结构化终态 `NativePlanProposal`。

具体实现应优先采用 `create_agent` 原生 structured response 能力。如果当前 Provider 对原生结构化终态存在兼容限制，应在共享 Provider adapter 边界内解决，不得另写 Planner 专用循环。

### 4.2 规划策略

Planner 采用自适应完整度：

- 对“查一下明天杭州天气”“给我到某地的驾车路线”这类明确单点请求，保持窄规划，避免机械扩展为酒店、餐饮等无关任务；
- 对“明天去安吉玩漂流”“帮我安排周末旅行”这类开放请求，主动识别影响决策和完整交付的维度；
- 领域 Skill 负责提供高质量的检查框架、Tool 使用顺序和完成标准，但不把领域规则硬编码进 runtime admission；
- Planner 可先查天气、目的地基础信息或其他高价值共享事实，再决定 DAG，而不是要求每项探索都变成 worker 节点；
- 独立且耗时的深度任务应尽量留给 DAG 并行执行，避免 Planner 串行包办全部工作。

这一区分主要依靠模型推理、Planner 系统提示词和已加载 Skill，不使用请求关键词路由。

## 5. 状态与数据契约

### 5.1 PlannerEvidence

Planner 的真实 Tool 调用结果不能遗失，也不能直接把完整消息历史无界传给所有 worker。规划状态新增有界、可引用的证据结构：

```text
PlannerEvidence
- evidence_id
- tool_name
- status
- structured_content 或 artifact_ref
- provenance/message reference
- created_at（如现有状态需要）
```

`PlannerEvidence` 只能从本次 Planner Agent 的真实 `ToolMessage` 或受信 artifact 转换得到。Planner 不得通过结构化输出自行伪造证据。超大结果沿用 artifact 引用，状态只保存有界摘要和引用。

### 5.2 NativePlanProposal

计划至少表达：

- `nodes`：可为空的任务节点集合；
- 每个节点的稳定 `node_id`、目标、依赖、允许 Tool、Skill/grant 需求；
- `evidence_refs`：该节点需要复用的 PlannerEvidence；
- `deliverables`：最终回答必须覆盖的交付项，以及负责生产它的节点或 PlannerEvidence；
- finalizer 所需的综合说明或输出约束。

零节点计划是合法的一等状态。当 Planner 已通过探索足以解决简单请求时，计划可以 `nodes=[]`，finalizer 直接使用 PlannerEvidence 回答，不能为了形式制造虚假 worker。

### 5.3 Worker 输入隔离

每个 worker 只接收：

- 原始用户目标的必要部分；
- 当前计划节点；
- 该节点的直接依赖结果；
- `evidence_refs` 明确引用的 PlannerEvidence；
- admission 后确认的 Tool allowlist 与 Skill grant snapshot。

不得把所有上游消息、全部 Planner Tool 结果或其他并行节点上下文默认广播给每个 worker。

## 6. Admission

`admit_plan` 是确定性治理边界，只验证可机器证明的结构和授权事实：

- `node_id` 唯一，依赖存在，DAG 无环；
- 节点引用的 Tool 存在于本次受信 inventory；
- Skill-governed Tool 具有 Planner 实际获得且允许继承的 grant；
- `evidence_refs` 指向真实捕获的 PlannerEvidence；
- deliverable 的 producer/ref 一致且可解析；
- 节点数、依赖深度和预算不超过配置上限。

Admission 不承担领域完整性判断。例如，若 Planner 根本没有声明“住宿”交付项，runtime 不通过关键词规则猜测用户在旅行并强制补酒店。领域完整度由 Skill、Planner 自检、coverage audit 和行为评测共同保证。

无效计划应返回结构化 admission error，并通过图内受限重试回到 Planner 修正；不得静默放宽授权或执行非法节点。

## 7. 原生 Scheduler

Scheduler 是 planning graph 中显式、确定性的 LangGraph 节点，不是 LLM Agent，也不是独立服务。每次进入时，它都从 checkpointed state 重新计算：

```text
ready = 未完成节点
        ∩ 所有直接依赖已成功
        ∩ 未处于运行/终止状态
```

随后用条件边和原生 `Send` 发出当前 ready wave。worker 结果经 reducer/join 写回 state，再次进入 scheduler 计算下一波。这样 checkpoint 恢复后无需恢复自定义内存队列，也不会出现数据库状态与 graph state 双写。

失败策略写成图策略，而非 scheduler 私有状态：

- 可重试 Tool/节点失败使用 LangGraph/Tool retry 能力和明确上限；
- 依赖永久失败的节点标记为 blocked/skipped；
- 若剩余证据仍可形成有价值回答，则进入 finalizer 并明确限制；
- 若需要修改任务分解，则经条件边进入 `coverage_audit -> planner_agent`，生成新 revision；
- revision 次数和总预算属于 graph policy，revision 内容保存在 `PlanningState`。

## 8. Finalizer

Finalizer 不再调用外部 Tool。它只读取：

- 原始用户请求；
- 已 admission 的计划与 deliverables；
- PlannerEvidence；
- worker 成功、失败和跳过结果；
- 必要的 Skill 输出格式约束。

它负责去重、处理证据冲突、标明不确定性，并形成用户可直接使用的完整回复。对于路线规划等结果，可消费 Tool adapter 已生成的高德路线规划链接；链接生成仍属于 Tool/MCP adapter 结果规范，不由 finalizer 猜测或拼接未经验证的数据。

## 9. 持久化、恢复与观测

所有状态均属于 `PlanningState`，由 LangGraph Agent Server 原生持久化：

- checkpoint 与 thread/run 生命周期；
- cancel、interrupt、resume；
- 节点重试与 time travel；
- Planner、admission、scheduler wave、worker、audit 和 finalizer 的事件树；
- LangSmith/LangGraph 原生 trace 关联。

不得为 planning 再实现影子 trace、任务表或恢复协议。Scheduler 在恢复后基于 checkpoint 重新推导 ready nodes；replan 只是 graph conditional edge，不是新的持久化系统。

## 10. 风险与约束

### 10.1 Planner 串行做完所有工作

这是高自由度方案的主要风险。解决方式不是重新限制 Planner 只能调用控制 Tool，而是通过系统提示词、调用预算和行为评测约束其角色：Planner 优先收集能改变计划或被多个节点复用的证据，把独立深挖任务交给并行 worker。

### 10.2 Tool 调用重复

计划节点通过 `evidence_refs` 复用 PlannerEvidence；finalizer 和 worker 不应重复查询仍然新鲜且满足任务需要的相同证据。对时效敏感或失败证据允许显式刷新。

### 10.3 Skill 与角色冲突

Planner 稳定系统契约优先于动态 Skill 指令。Skill 可以规定领域流程、检查项和 Tool 方法，但不能让 Planner 绕过 admission、直接面向用户结束、扩大 Tool grant 或创建自定义执行循环。

### 10.4 高自由度带来的成本

分别记录 Planner 探索、worker 执行和 finalizer 的调用量、延迟与 Tool 重复率；配置各阶段预算，并允许窄请求走零节点或小 DAG。不得用硬编码意图路由换取成本下降。

## 11. 验证策略

默认验证全部使用 mock/local/offline。

结构和确定性测试至少覆盖：

1. Planner 与 fast agent 的首轮 Tool projection 相同；
2. Planner 可调用默认 Tool，也可先加载 Skill 再调用受治理 Tool；
3. PlannerEvidence 只来自真实 ToolMessage/artifact；
4. admission 拒绝未知 Tool、伪造 evidence ref、无 grant Skill Tool 和有环 DAG；
5. scheduler 正确生成并行 wave，并能从 checkpoint state 重新推导下一波；
6. worker 只获得直接依赖和明确引用证据；
7. `nodes=[]` 时直接进入 finalizer；
8. replan 通过原生 graph state 和 conditional edge 完成；
9. planning 非 read Tool 保留 HITL，fast 模式行为不被改变；
10. finalizer 无 Tool 执行能力。

行为质量评测需在用户明确启用 real mode、真实 Provider 配置完整并通过 operator 开关时单独运行，至少比较：

- 开放旅行请求是否形成天气、玩法/攻略、交通、住宿或明确不住宿的判断、餐饮和相关景点等自适应覆盖；
- 明确的单点驾车路线请求是否保持窄范围；
- Planner 是否过度串行调用 Tool；
- worker 是否获得有效并行度并复用证据；
- 最终回复的完整性、可执行性、延迟和重复调用率。

旅行示例是行为评测样本，不是 admission 的硬编码业务规则。

## 12. 非目标

本阶段不做以下事项：

- 自研 Planner Runtime、scheduler service 或 checkpoint 层；
- 基于关键词选择 Tool、Skill 或 workflow；
- 在 admission 中固化“旅行必须查酒店”等领域规则；
- 默认启用真实 Provider；
- 让 Planner 或 finalizer 绕过既有 Tool adapter 直接调用外部服务；
- 为追求 DAG 形式而强制生成无意义节点；
- 同时重构与本目标无关的 fast agent、Memory、媒体或入口层。

## 13. 最终设计结论

Planner 与 fast agent 共享同一个原生 Agent 基座和首轮 Tool 自由度，但 Planner 的高优先级系统提示词将其角色锁定为“探索后规划”：它可以先行动，再输出结构化计划，不能直接替代 finalizer 作答。Skill 提升其领域认知和工具使用上限，PlannerEvidence 保存真实探索成果，admission 只守结构与授权，scheduler 以原生 `Send` 确定性执行 DAG，全部状态和恢复交给 LangGraph Agent Server，finalizer 汇总 Planner 与 worker 的真实证据形成完整回复。
