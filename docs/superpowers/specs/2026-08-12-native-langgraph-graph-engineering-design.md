# 原生 LangGraph Graph 工程演进设计

日期：2026-08-12

## 1. 背景与问题

当前项目虽然以 LangGraph `StateGraph` 承载普通 assistant loop，但 LangGraph 只负责很薄的三节点
ReAct 循环。流式、运行身份、取消、恢复、trace、评估以及 Durable Workflow DAG 的推进、并行、join、
lease、retry 和状态合并主要由项目自行实现。结果是项目事实上形成了一套自定义 Agent 框架：新增 DAG
能力时经常需要同时修改 scheduler、状态机、事件桥、trace exporter、评估 runner 和多套存储映射，
LangGraph 与 LangSmith 的原生协同也无法自然发挥。

本次演进不以局部替换某个 runner 为终点，而是纠正运行内核在开发过程中发生的架构偏移。

## 2. 总目标

> 以 LangGraph 原生能力建设真正的 Graph 工程，而不是继续维护一个“底层用了 LangGraph、上层却是
> 自研框架”的系统。

Graph 必须成为执行结构和执行位置的唯一事实源。开发一个新 DAG 时，主要工作应当是定义 state、node、
edge、subgraph、reducer、interrupt 和 evaluator，而不是继续开发通用 scheduler、恢复状态机和远端
trace tree 重建逻辑。

“原生态”不表示机械使用 LangGraph 的每一个 API，而遵循以下判断标准：

- 属于通用 Graph Runtime 的职责，优先由 LangGraph 原生机制承担；
- 只有明确的产品、协议、安全或领域需求才由项目扩展；
- 每引入一项原生能力，必须冻结或删除对应自研能力，不能长期维护双轨实现；
- LangSmith 应自然展示并评估实际执行的 graph，而不是观察项目手工重建的影子执行树。

目标结果包括：

- `AssistantTurnGraph` 成为可独立运行、流式、持久、恢复、测试、评估和嵌套复用的基础 Agent 子图；
- `DurableWorkflowGraph` 原生组合 planner、worker、verifier 等 `AssistantTurnGraph` profile；
- 原生使用 state、reducer、conditional edge、`Send`、subgraph、async stream、checkpoint、
  `interrupt`/`Command(resume=...)` 和持久恢复；
- LangSmith 成为主要 trace、调试、Dataset、Experiment 和 evaluation 平台；
- Langfuse 在迁移期只保持兼容，达到等价验收后退出；
- 删除自研 DAG scheduler、影子状态机、重复事件流、远端 trace 投影和无人依赖的兼容抽象。

### 2.1 Graph API 原生开发硬约束

后续 M2–M5 始终围绕 LangGraph Graph API 的真实执行语义开发，而不是在自研框架中仅调用一层
LangGraph。实现与验收应按场景使用并核对：`StateGraph`、`State`、`Node`、`Edge`、`START`、`END`、
`Conditional Edge`、`Command`、`Send`、`Reducer`、`Subgraph`、Pregel / Super-step、`Compile`、
`Invoke`、`Stream`、`Checkpoint`、`Checkpointer`、`Thread`、`Interrupt`、`Resume`、`Memory`、`Store`、
`Runtime Context`、`Retry Policy`、`Timeout`、`Fallback`、Streaming Modes、Time Travel、Replay 与 Fork。

该清单不是要求为覆盖名词而机械调用 API；要求是：只要项目自研层正在承担其中已有的通用 Graph
Runtime 职责，默认迁移到对应原生机制，并删除或降级原实现为薄产品适配。每个里程碑的测试必须观察
实际 compiled graph、stream、checkpoint 或 state history 事实，不能用旁路模拟器证明原生能力已经接入。

## 3. 兼容范围与非目标

### 3.1 必须保护

- Agent-Service 已强依赖的外部协议；
- 媒体 API 和对应交付语义；
- Tool 的安全、授权、schema、幂等和副作用治理；
- 用户身份隔离、artifact ownership 和必要业务审计；
- mock/real Provider 显式隔离及默认离线安全边界。

### 3.2 允许 breaking cleanup

除上述真实消费者外，不默认保护内部 Runtime API、trace schema、DAG scheduler API、eval runner、
未被真实客户端使用的 HTTP API 或兼容抽象。迁移优先保证真实产品行为，而不是维护历史内部实现。

### 3.3 非目标

- 不把 WebSocket 连接、媒体编解码或 Gateway delivery 生命周期塞入 LangGraph state；
- 不用 LangSmith 代替业务数据库、checkpoint 或审计事实源；
- 不用裸 `ToolNode` 绕过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`；
- 不在一次迁移中大爆炸重写全部 Runtime；
- 不让 Workflow v2 的产品 schema 暴露 `Send`、task ID 或 checkpoint ID 等 LangGraph 内部字段。

## 4. 目标架构

```text
Agent-Service / Media API
        │
        ▼
薄入口适配与产品事件投影
        │
        ▼
LangGraph Graph Family
        │
        ├── AssistantTurnGraph
        │     prepare_run / context
        │          ↓
        │     assistant ←────────────┐
        │          │                 │
        │          ├→ governed_tool ─┘
        │          ├→ interrupt → resume
        │          └→ compose / commit
        │
        └── DurableWorkflowGraph
              admit → planner subgraph
                    → fan-out worker subgraphs
                    → join → verifier subgraph
                    → repair 或 publish
        │
        ├── Provider ports
        ├── Tool governance
        └── memory / media / artifact / audit services
        │
        ▼
LangSmith 原生 graph / node / subgraph / LLM / tool trace 与评估
```

### 4.1 AssistantTurnGraph

`AssistantTurnGraph` 是最小、稳定、可复用的 Agent 执行内核。普通对话直接运行该图；Workflow 的
planner、worker 和 verifier 通过不同 profile 复用该图。Provider、Tool、context、stream、checkpoint
和 interrupt 均应在真实节点边界上可见。

领域服务不因成为节点而进入 checkpoint。节点通过 LangGraph runtime context 获取 Provider adapter、
Tool executor、memory/media service、artifact store 等运行依赖。

### 4.2 DurableWorkflowGraph

`DurableWorkflowGraph` 是主要高层组合图。它负责 plan admission、并行 worker、join、verification、
repair 和终态路由。LangGraph 负责节点推进、条件边、并行 superstep、pending task、checkpoint 和 resume；
项目不再实现第二套通用 DAG scheduler。

已完成的 Workflow v2 保留领域语义，包括 submission、plan/node schema、acceptance contract、constraint、
deliverable、artifact、ownership、budget 和 product progress。迁移原则是：

> Workflow v2 保留“它描述什么”，LangGraph 接管“它如何执行”。

## 5. 职责边界

### 5.1 LangGraph

- graph、node、edge、conditional routing 和 subgraph；
- state channel 与 reducer；
- `Send` fan-out、并行 superstep 和 join；
- async stream；
- checkpoint、state history、interrupt、resume 和故障恢复；
- graph/task/subgraph 执行身份和执行位置。

### 5.2 LangSmith

- graph、node、subgraph、LLM 和 governed tool 的主要 trace；
- Dataset、Experiment、Feedback、在线与离线 evaluator；
- final response、trajectory 和 single-node evaluation；
- Graph 开发期调试、对比和回归分析。

LangSmith 是观测和质量平台，不是调度或恢复事实源。

### 5.3 项目领域层

- Agent-Service 和媒体协议；
- Provider adapter；
- Tool policy、validator、executor、registry 和副作用治理；
- memory、media、artifact 等领域服务；
- 用户隔离、授权、业务幂等和业务审计；
- 产品查询视图与实际交付状态。

### 5.4 数据存储

- LangGraph checkpointer：执行位置、state、pending task、interrupt 和 subgraph 恢复信息；
- 业务数据库：submission、owner、artifact、审计、副作用幂等和产品查询摘要；
- LangSmith：trace、Dataset、Experiment 和 Feedback；
- Langfuse：仅迁移期兼容，最终退出。

## 6. State 与身份设计

### 6.1 身份模型

| 身份 | 生命周期 | Owner |
| --- | --- | --- |
| `session_id` | 用户产品会话，可包含多个 turn | Agent-Service / Gateway |
| `thread_id` | 一条可恢复的 LangGraph 执行线程 | LangGraph |
| `run_id` | 一次 graph invoke 或 resume | Runtime |
| `workflow_id` | 一个长期业务 Workflow | Workflow domain |

普通对话使用稳定 conversation `thread_id`，每个 turn 产生新的 `run_id`。一个 Durable Workflow 使用独立
Workflow `thread_id`；planner、worker 和 verifier 由 checkpoint namespace / subgraph path 区分。
LangSmith 关联这些身份，但不拥有业务身份。`trace_id` 不再作为内部调度主键。

### 6.2 AssistantTurnState

只保存恢复所需事实，例如 messages/current request、context reference、capability reference、tool
observation、phase/status、pending interrupt、final response 和安全错误摘要。

Provider client、Tool registry/executor、数据库连接、memory/media service、event sink、callback、cancel
token、大型媒体内容和 artifact 正文不得进入 checkpoint。媒体与 artifact 只保存稳定引用。

### 6.3 DurableWorkflowState

保存 workflow ID、submission reference、admitted plan、节点执行结果、artifact reference、repair state、
budget consumption 和 terminal state。业务数据库不再计算下一个 ready node；checkpointer 不保存业务
artifact 正文。

Workflow v2 通过明确转换进入 graph state：

```text
WorkflowSubmission
  → Workflow v2 planner proposal
  → admission / validation
  → AdmittedWorkflowPlan
  → DurableWorkflowState
```

### 6.4 并行 reducer

worker 返回独立、按 `node_id` 定位的 `WorkerResult`。父图 reducer 必须顺序无关、重放幂等、能识别重复
结果，并在冲突时显式失败。并行 worker 不得共同原地修改一个大 state 对象。

### 6.5 State 版本

持久 state 明确携带或能够确定 `graph_name`、`graph_version` 和 `state_schema_version`。改变持久字段语义
必须升级 schema，并在部署前选择迁移旧 checkpoint、由旧 graph 完成或明确终止；新 graph 不得静默读取
不兼容 checkpoint。

## 7. 执行、流式与产品事件

Runtime facade 直接消费 LangGraph v2 async stream，并按需启用 `updates`、`messages`、`custom`、`tasks`
和 `checkpoints`，同时允许 subgraph stream。主路径不再使用同步 `invoke() + asyncio.to_thread`。

Agent-Service 不直接暴露全部 LangGraph 事件。薄 `ProductEventProjector` 只投影真实客户端需要的 run
started、text delta、tool/product progress、waiting input、final、cancelled 和 failed。checkpoint、内部 task、
完整 state 和 graph debug event 不进入公共协议。

Projector 只能单向投影已经发生的执行事实，不能决定下一节点、推进 Workflow 或成为恢复事实源。

LangSmith 直接观察 compiled graph。自定义领域 metadata 或 `traceable` 调用只补充必要语义，不重新构造
平行 span tree。安全审批、幂等提交、artifact ownership 和媒体实际送达仍使用独立业务审计，不伪装为
LangGraph node。

## 8. 失败、Interrupt 与恢复

错误分为：

- 瞬时执行失败：节点内有界 retry 或从 checkpoint 重试；
- 等待外部输入：通过 `interrupt()` 持久等待；
- 业务拒绝：形成结构化结果，经条件边进入 repair 或 fail；
- 基础设施损坏：checkpoint 不兼容或 state corruption 时 fail closed。

产品终态和 LangSmith trace 必须区分 completed、interrupted、cancelled、rejected、failed 和 infrastructure
error，不能把所有异常转成自然语言兜底后标记 graph success。

恢复使用相同 `thread_id` 和 checkpoint，通过 `Command(resume=...)` 继续。考虑到节点可能从开头重新执行：

- interrupt 前不得执行不可重复副作用；
- 写 Tool 必须具有 operation/idempotency key；
- artifact 写入使用稳定引用或内容摘要；
- publish/delivery 使用 commit barrier；
- 已成功的外部副作用通过业务幂等记录短路重复执行。

`interrupt` 表示业务等待且可恢复；`cancel` 表示终止当前执行；transport disconnect 只停止订阅，不自动取消
Workflow；Gateway 继续拥有连接和 replacement 生命周期，LangGraph 拥有执行位置和恢复。

## 9. LangSmith 评估模型

LangSmith 成为唯一新增能力的 trace/eval 目标，评估分为三层：

1. 单节点：planner plan、assistant Tool 决策、verifier acceptance 判断；
2. AssistantTurnGraph：最终回答、Tool trajectory、参数顺序、失败修正、interrupt、grounding 和质量；
3. DurableWorkflowGraph：完整 DAG trajectory、fan-out 覆盖、join、repair 最小化、artifact/constraint 完整、
   resume 等价性、latency、token 和失败率。

Dataset target 直接运行 compiled graph 或薄 Runtime facade，不再装配第二套 eval runtime。现有 Release
Review YAML scenario、Decision fixture backend、Staging 隔离资源、task conformance 和评分语义可以保留，
平台绑定改为 LangSmith Dataset、Experiment 和 Feedback。

Runtime Regression 与 Release Review 继续使用不同 Dataset 和运行目的，不合并为一个总分。

迁移期可以双写 LangSmith/Langfuse，但不得新增统一双平台 abstraction，不要求两边 trace tree 一致，且
每个双写点必须有删除里程碑。LangSmith 是开发诊断依据；Langfuse 只验证旧消费者未受影响。

## 10. 渐进迁移里程碑

### M1：LangGraph/LangSmith 原生基线

- 稳定编译 `AssistantTurnGraph`；
- runtime context 注入；
- 原生 async stream；
- LangSmith 直接展示 graph/node/LLM/tool；
- 建立最小 Dataset/Experiment；
- 明确 session/thread/run/workflow identity；
- 冻结 Langfuse、手工 trace tree 和 Workflow v2 scheduler 的新能力开发。

### M2：可嵌套、可恢复的 AssistantTurnGraph

- 最小可序列化 state；
- 持久 checkpointer；
- subgraph profile；
- `interrupt`/resume；
- Provider/Tool 稳定 trajectory；
- 删除同步线程桥主路径和模拟 graph lifecycle 的重复事件。

该阶段只需完成 Workflow 纵切所需的公共内核，不要求先完成全部普通对话高级能力。

### M3：Workflow v2 LangGraph 纵切

优先迁移 Deep Research：

```text
submission → planner AssistantTurnGraph → v2 admission
           → parallel worker AssistantTurnGraph
           → join → verifier AssistantTurnGraph
           → repair 或 publish
```

完成并行、join、repair、checkpoint/resume 和完整 LangSmith trace。纵切完成后，旧 scheduler 不再执行该
Workflow 类型。

### M4：Workflow 全量迁移与双轨收缩

- 迁移全部正式 Workflow definition；
- 旧 Workflow scheduler 仅作为有截止点的迁移 fallback；
- Workflow API 改为 graph state/task 的产品只读投影；
- 删除 dependency-wave、ready-node 推进、通用 execution lease/CAS 和重复恢复状态。

SQLite 仅保留业务记录、artifact、审计、幂等和必要查询视图。

### M5：全面收敛

- 完善普通对话的 state history、time travel 和必要 interrupt；
- LangSmith Release Review 与 Runtime Regression 达到等价验收；
- 删除 Langfuse runner、webhook、exporter、配置、文档和依赖；
- 删除仅为远端 trace tree 服务的 canonical trace 投影；
- 收缩 `AgentGraphRuntime` 为薄 facade，或由 compiled graph application 取代；
- 删除无人依赖的 API 和兼容抽象。

## 11. 测试与验收

### 11.1 确定性 pytest

迁移会影响 `RUN-001`、`LOOP-001`、`IDENT-001`、`DUR-001` 和 `OBS-001`；Agent-Service/媒体兼容继续由
`GATE-001` 等既有契约保护。每个里程碑先在独立 `tests/tdd/<feature>/` 做 mock/local/offline 的临时
RED/GREEN，再只把真正长期稳定的最小结构化契约回补到已有 core invariant 负责文件。具体 Workflow
definition、prompt、文案和第三方框架自身行为不进入 core。

### 11.2 每阶段证据

每个里程碑必须同时提供：

1. 确定性 pytest 的结构化框架契约；
2. LangSmith Experiment 的真实 Agent 行为证据；
3. Agent-Service/媒体受保护协议的兼容证据；
4. 对应旧实现的删除清单或明确截止里程碑。

不能用“新路径已能运行”代替“旧重复路径已经退出”。

### 11.3 完成判据

总目标完成必须满足：

- 普通 turn 和 Durable Workflow 都由可观察、可恢复、可组合的 LangGraph graph family 执行；
- Durable Workflow 的并行、join、repair 和 resume 不依赖项目自研通用 scheduler；
- LangSmith 原生呈现并评估实际 graph 层级；
- Agent-Service 与媒体外部行为保持兼容；
- 工具安全和副作用治理未被绕过；
- Langfuse 与重复 trace/eval/runtime 基础设施已退出；
- 新 DAG 开发不再要求同步维护影子状态机、调度器和远端 trace tree。

## 12. 设计决策摘要

- 采用“先原生观测与基础图，再尽早做 Workflow v2 纵切，最后全量收敛”的渐进路线；
- `AssistantTurnGraph` 是基础 Agent 子图，`DurableWorkflowGraph` 是重点高层组合图；
- Workflow v2 领域模型保留，通用执行职责迁给 LangGraph；
- LangSmith 是最终主 trace/eval 平台，Langfuse 冻结并退出；
- 只保护 Agent-Service 强依赖协议、媒体 API、安全与必要领域事实，其余允许 breaking cleanup；
- 迁移以删除重复实现为完成条件，不以新增兼容层为完成条件。
