# 通用长阶段任务能力实施路线图

> **For agentic workers:** 本文只负责跨实施包的边界、依赖和验收门禁，不可作为一次性大改计划执行。开始编码前必须选择一个对应的详细实施计划，并使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务执行；步骤使用 checkbox（`- [ ]`）跟踪。

**Goal:** 在不替换现有 `AgentGraphRuntime`、不增加入口意图分类器的前提下，为 Agent 增加可跨进程恢复、可局部返工、可管理上下文和 artifact 的通用 long-horizon workflow 能力；深度研究只是第一个业务定义。

**Architecture:** 现有 Provider-native ReAct LLM 仍是唯一的工具选择者，并在适合时自主调用受治理的 `workflow_submit` Tool。Tool 只创建持久 Workflow；独立 worker 通过确定性 `DurableWorkflowRuntime` 推进阶段和 work item，每个语义 work item 仍回到现有 `AgentGraphRuntime` 有界执行。业务状态、LangGraph checkpoint、artifact/workspace、prompt context 和事件审计分别持久化。

**Tech Stack:** Python 3.11、Pydantic v2、现有 `AgentGraphRuntime` / Tool Plugin / `ActionValidator -> ToolExecutor -> ToolRegistry` 治理链、LangGraph、SQLite、pytest 临时 TDD、现有 observability 与 Gateway/API 薄入口。

## Global Constraints

- 已确认决定只有两项：LLM 自主选择专用 Tool 开启长阶段任务；能力必须通用，不能只为深度研究定制。
- `workflow_submit` 是否进入 Tool catalog 只能由显式配置、可信 entry capability、已绑定服务和可用 `WorkflowDefinition` 等结构化事实决定；不得使用关键词、正则、启发式话术或前置分类 LLM。
- 普通任务继续运行当前 Provider-native assistant loop；不引入第二套主 Agent loop、Provider client 或 ToolExecutor。
- `DurableWorkflowRuntime` 是确定性 controller，不拥有独立 LLM；规划、执行、语义验证需要模型时，通过现有 `AgentGraphRuntime` 的受限入口调用同一 Provider 治理层。
- Gateway 只拥有一次前台 ingress run；长期 Workflow 不长期占用 Gateway active run、WebSocket 或 coroutine。
- 所有本地显式 Tool 调用仍经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`；首个版本只允许 LLM、只读 Tool 和受管 artifact 写入。
- `workflow_id`、`work_item_id`、`attempt_id`、`run_id` 分离；FlowStore revision、worker lease 和 attempt action intent 必须可审计。
- graph checkpoint 只保存恢复 cursor 和最小 projection；大结果、来源正文和报告草稿保存为不可变 artifact，不进入无界消息历史。
- 每个实施包必须保持 mock/local/offline；不得调用真实 Provider、联网或安装新依赖。
- 新功能的 RED/GREEN 只进入独立 `tests/tdd/durable-workflow-*/`；当前不修改 `tests/core` 或 `tests/core/INVARIANTS.md`。能力稳定后若要扩展 `DUR-001`、`TOOL-001`、`CTX-001` 或 `OBS-001`，必须另做明确 core invariant 决策。
- 不在功能提交中顺手删除 legacy runtime。清理只能在通用能力稳定后按独立迁移计划进行，每批独立验证、独立提交。
- 每个阶段完成后先同步对应根级权威文档，再进入依赖它的下一阶段；实验设计文档不能替代当前事实文档。

---

## 1. 决策状态

| 主题 | 状态 | 当前约束 |
| --- | --- | --- |
| 长流程入口 | 已确认 | LLM 在现有 ReAct 中自主调用专用 Tool |
| 能力范围 | 已确认 | 通用 long-horizon workflow；Research 只是首个 definition |
| 独立 LLM | 已否定 | controller 不拥有独立 LLM，复用现有 Provider/runtime |
| 入口分类器 | 已否定 | 不新增 assistant decision、关键词路由或前置分类 LLM |
| Store schema、LangGraph 节点和恢复细节 | 待实施验证 | 允许在不违反前两项决定的前提下按阶段调整 |
| legacy 删除范围 | 待调用面审计 | 只能在新路径稳定后单独迁移与删除 |

## 2. 实施包依赖

```text
P1 通用契约与持久状态底座
 ├──> P2 workflow_submit 与结构化 admission
 └──> P3 持久 WorkflowGraph 与 deterministic worker
          └──> P4 Artifact workspace 与 Workflow Context
                    └──> P5 AgentGraphRuntime.run_work_item
                              └──> P6 Deep Research 首个垂直定义
                                        └──> P7 局部返工、waiting_input 与恢复
                                                  └──> P8 并发、产品入口与评测

P9 legacy runtime 清理：只依赖 P1—P8 的稳定调用面，不与任何功能包并行混改。
```

P2 与 P3 都依赖 P1，但可以先后开发、不可同时修改 P1 的公共 schema。P2 即使实现完成，也必须保持 fail-closed：没有可执行 definition、worker binding 或 feature enablement 时不得向 LLM 暴露 `workflow_submit`。

## 3. 实施包清单

### P1：通用契约与持久状态底座

详细计划：[`2026-08-07-durable-workflow-foundation.md`](2026-08-07-durable-workflow-foundation.md)

范围：

- 新建 `assistant_agent.workflows` 包；
- 定义通用 Workflow、Plan、WorkItem、Event、Budget、Lease 契约；
- 定义 `WorkflowDefinition` / catalog，不加入 Research 专有字段；
- 实现 InMemory/SQLite Store、revision、lease、event cursor；
- 实现不对 runtime 暴露的 identity-scoped service；
- 使用测试内 `ProbeWorkflowDefinition` 验证重启恢复和幂等提交。

明确不做：Tool 注册、LangGraph、worker、Provider 调用、artifact 正文、API/Gateway、Research。

退出门禁：schema/DAG/终态不变量、OCC、身份隔离、提交幂等、lease reclaim 和 SQLite reopen 全部通过临时离线测试；生产 Tool catalog 与 runtime 行为完全不变。

### P2：`workflow_submit` 与结构化 Admission

拟定详细计划：`docs/superpowers/plans/2026-08-07-durable-workflow-submit-tool.md`

范围：

- 新增 `WorkflowAdmissionPolicy`，将用户请求预算收紧为系统预算；
- 新增 `workflow_submit` builtin Tool Plugin 和 Tool ID；
- 扩展 `ToolPluginContext`，注入可信 Workflow service/capability；
- 在默认 registry 装配中按结构化事实 fail-closed；
- 将 `workflow_id/status/status_url/event_cursor` 作为结构化 observation 返回给现有 ReAct；
- admission 拒绝后允许当前 assistant loop 继续，不替模型做 fallback 决策。

明确不做：前置分类器、关键词 Tool 预选、Graph worker、Research 语义、API 状态路由。

退出门禁：LLM 可通过 native tool call 提交 Probe Workflow；普通文本/普通 Tool 路径不变；重复 native call 幂等；所有调用经过 Tool 治理链；未完成 worker binding 时 Tool 不暴露。

### P3：持久 WorkflowGraph 与确定性 Worker

拟定详细计划：`docs/superpowers/plans/2026-08-07-durable-workflow-graph-worker.md`

范围：

- 为 Workflow 使用独立的持久 LangGraph checkpointer factory；
- 实现最小 graph state 与 `hydrate -> reconcile -> guard -> ensure_plan -> select -> prepare -> commit -> yield` 主干；
- 实现 `DurableWorkflowWorker`、单 quantum 推进和 startup/shutdown 托管；
- 每个 quantum 最多提交一个 work item 终态或一个 plan revision；
- 用确定性 Probe definition 验证 FlowStore 与 graph cursor 对账。

明确不做：真实 LLM work item、Research、全文 artifact、并发 work item。

退出门禁：任一侧 checkpoint 落后都可收敛；进程在 prepare/commit 前后崩溃均不重复业务提交；旧 lease 不能覆盖新 revision；停止 worker 不丢已提交状态。

### P4：Artifact Workspace 与 Workflow Context

拟定详细计划：`docs/superpowers/plans/2026-08-07-durable-workflow-artifact-context.md`

范围：

- 实现 owner-bound 不可变 `ArtifactRef`、digest、producer lineage 和 retention tombstone；
- 建立 workflow workspace，区分 source、evidence、draft、report 等通用 kind，不把 Research schema 写入 store 基类；
- 定义 `WorkflowContextManifest`、artifact excerpt provider 和严格 token/char budget；
- 为大 Tool observation 增加受管 offload 扩展点，但不改变普通任务默认 observation；
- 为 work-item handoff 保存结构化摘要和 artifact refs。

退出门禁：重启恢复不依赖完整 transcript；跨 owner artifact 读取被拒绝；重复写使用 digest 幂等；prompt projection 不加载完整语料。

### P5：现有 Runtime 的有界 Work-item 执行入口

拟定详细计划：`docs/superpowers/plans/2026-08-07-agent-runtime-work-item-entry.md`

范围：

- 新增 `AgentWorkItemRequest` / `AgentWorkItemResult`；
- 在 `AgentGraphRuntime` 增加 `run_work_item()`，复用 assistant loop、ContextService、Provider adapter、ToolExecutor、事件和 trace；
- 按 definition/step policy/部署事实构造允许的 Tool 候选空间；
- 对 iteration/tool/token budget 和结构化 terminal output 做确定性校验；
- 将 workflow/work-item/attempt 身份绑定到可信 runtime metadata。

明确不做：第二个 Provider client、独立 Research LLM、直接从 controller 调 Tool、改变普通 `run_state()` 契约。

退出门禁：Probe work item 通过现有 ReAct 完成；普通请求无新增 context；Tool governance/身份审计不被绕过；大 observation 可交给 P4 offload。

### P6：Deep Research 首个 `WorkflowDefinition`

拟定详细计划：`docs/superpowers/plans/2026-08-07-deep-research-workflow-definition.md`

范围：

- 实现 Research submission schema、scope、decompose、collect、source review、evidence、outline、draft、verify、synthesize；
- 使用现有受治理 read Tool，不创建 Research 专属 Provider client；
- claim/evidence/source/artifact 全链路可追溯；
- 完成 10—30 个 mock source、5—10 个章节的中型场景；
- 首次在生产候选 catalog 中提供一个真实 `workflow_type`。

退出门禁：带引用报告可从中间阶段跨重启继续；无来源 claim 被结构化 verifier 拒绝；单来源失败不导致全量重跑；普通非 Research 请求仍由 LLM 自主决定是否调用 Tool。

### P7：局部返工、`waiting_input` 与错误恢复

拟定详细计划：`docs/superpowers/plans/2026-08-07-durable-workflow-recovery.md`

范围：

- 定义 `retry_same`、`repair_children`、`replan_subtree`、`need_user`、`terminal_fail` disposition；
- 实现依赖失效传播、plan version 和 attempt budget；
- 实现 waiting input request、resume token、幂等 input 提交；
- 实现 no-progress、budget exhaustion、deadline、cancel 和 unknown outcome 对账；
- 返工只创建新 plan version 或 repair item，不覆盖历史 artifact。

退出门禁：每种错误都有结构化恢复路径和最大重试上限；用户输入只恢复目标 interrupt；无关成功子图不被重跑；取消后旧 worker 无法提交。

### P8：受限并发、产品入口、观测与评测

拟定详细计划：`docs/superpowers/plans/2026-08-07-durable-workflow-productization.md`

范围：

- 最多 3 个 read-only child work item 并发及 revision conflict 收敛；
- Workflow status/events/input/cancel API 与 Gateway/CLI 薄入口；
- event cursor replay、慢消费者和断线恢复；
- canonical workflow/work-item/attempt events、trace links、backlog/recovery 指标；
- `evals/agent` 增加“该不该提交 Workflow”和完成质量任务；真实 Provider 验证另走受控 system/Agent eval。

退出门禁：前台连接生命周期与 Workflow 解耦；并发不破坏幂等；状态和最终 artifact 可被身份隔离地查询；模型不会因 Tool 存在而被入口规则强制创建 Workflow。

### P9：Legacy Runtime 独立清理

拟定详细计划集合：

- `2026-08-07-runtime-tool-call-naming-migration.md`
- `2026-08-07-mock-native-tool-call-migration.md`
- `2026-08-07-remove-conditional-runtime.md`
- `2026-08-07-remove-plan-and-solve-compatibility.md`
- `2026-08-07-durable-task-convergence-audit.md`

顺序：

1. 内部 `AssistantDecision` 命名迁移，保留 deprecation shim；
2. 用 scripted Provider native outputs 取代 mock planner/router 依赖；
3. 关闭并删除 conditional/legacy graph；
4. 迁移 `plan_and_solve` 跨层兼容协议；
5. 根据真实复用证据决定旧 DurableTask 是保留 automation、共享 primitive 还是迁移 definition。

退出门禁：每一批均有调用面审计、兼容窗口、独立验证和独立提交；不得以 Workflow 测试代替普通 assistant loop 回归。

## 4. 跨实施包公共契约所有权

| 契约 | 首次定义 | 后续修改规则 |
| --- | --- | --- |
| Workflow/Plan/WorkItem/Event/Lease | P1 | P2—P8 只能向后兼容扩展；破坏性修改回到 P1 schema gate |
| `WorkflowDefinition` | P1 | P6 可新增 Research 实现，不得把 Research 字段加进 Protocol |
| `workflow_submit` Tool schema | P2 | P6 只能注册新 `workflow_type`，不得创建 Research 专用提交 Tool |
| graph state / checkpoint | P3 | P4—P8 只保存 refs/cursor，不保存正文或 runtime client |
| Artifact/Context manifest | P4 | P5—P8 通过 Protocol 消费，不绕过 owner/budget 校验 |
| `run_work_item()` | P5 | P6—P8 通过 request/result contract 使用，不导入 runtime 私有 node |
| Research models | P6 | 只能位于 `workflows/research/`，不能反向污染通用层 |
| API/Gateway projection | P8 | 只调 service，不直接读 SQLite、checkpointer 或 workspace 文件 |

## 5. 每个实施包的固定执行节奏

- [ ] 先确认前置实施包的退出门禁和工作树状态。
- [ ] 只读取该实施包需要的权威文档和源码，不把历史计划当成当前事实。
- [ ] 在自己的 `tests/tdd/durable-workflow-<stage>/` 写最小 RED。
- [ ] 实现一个可观察契约，运行同一文件得到 GREEN。
- [ ] 每完成一个内聚 task，运行该 task 的最小显式测试并独立提交。
- [ ] 完成本包后运行本包完整 TDD；只有影响已登记 invariant 时才运行对应 core 测试。
- [ ] 同步根级权威文档，记录哪些设计已成为当前事实。
- [ ] 审核普通 assistant loop、现有 DurableTask 和 Gateway 是否保持本包声明的不变项。
- [ ] 记录未实现项，不跨包“顺手完成”。

## 6. 暂停与回退门禁

出现以下任一情况时暂停当前实施包，不继续扩 scope：

- 需要用关键词/正则才能决定是否暴露或调用 Workflow Tool；
- 需要让 Gateway 长期持有 active run 才能恢复 Workflow；
- 需要 controller 直接调用 Tool 或创建第二套 Provider client；
- 需要把网页正文、完整 transcript 或 client 对象写进 graph checkpoint；
- SQLite/FlowStore 与 LangGraph checkpoint 无法在无分布式事务下通过 reconcile 收敛；
- 为完成 Research 被迫把 Research 字段写进通用 Workflow model；
- 旧 DurableTask schema 必须破坏性修改才能推进新路径；
- core invariant 需要变化但尚未明确登记和批准。

安全回退策略始终是：关闭 Workflow feature/candidate exposure，保留已经持久化的记录供只读诊断；普通 Provider-native assistant loop 不回退、不替换。
