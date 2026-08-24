# AI Coding Stage 5C：独立 Code Review Graph 设计

## 1. 目标

Stage 5C 在 coding mutation lane 的确定性验证成功之后、commit 或 terminal 之前，引入独立的只读
`AssistantCodingReviewGraph`。它针对最终累计 diff 生成结构化审查报告，并通过原生 HITL 让用户明确批准或拒绝；
模型不能自动授权 commit、merge 或任何写操作。

本阶段延续 Stage 5B 的不可变快照、固定并行 inventory、确定性 join、checkpoint 绑定和资源硬上限，
但 review 使用独立状态与输出契约，不复用初始分析结果充当最终审查。

## 2. 非目标

- 不让 reviewer 生成、应用或建议可执行 patch。
- 不新增 reviewer 驱动的自动修复、`respond` 或循环重试。
- 不让 reviewer 执行 shell、访问网络、调用 Provider-native search 或写 workspace。
- 不创建第二套 Agent Runtime、独立 Agent Server run 或旁路 integration service。
- 不改变现有 validation repair budget、merge approval 或 worktree 生命周期所有权。
- 不把临时 TDD 自动晋升为 `tests/core`。

## 3. 总体拓扑

```text
run_validation
    ├─ failed -> 现有 repair / terminal
    └─ passed
         ├─ code_review_enabled=false -> 现有 create_commit / terminal
         └─ code_review_enabled=true
              -> prepare_review_snapshot
              -> AssistantCodingReviewGraph
                   -> prepare_review_tasks
                   -> Send(review_workspace) x 3
                   -> join_review
              -> coding_review_decision
                   ├─ approve -> create_commit / terminal
                   └─ reject  -> rejected terminal
```

Review Graph 是父 `AssistantCodingGraph` 内部的 compiled subgraph。父图与子图 schema 不同，因此父节点通过
窄 wrapper 映射 `CodingReviewInput` 和 `CodingReviewReport`；子图默认 `checkpointer=None`，每次调用隔离，
同时继承父图 checkpointer 以支持同一次调用内的 durable execution。

所有 mutation 继续位于父图现有单线程 lane。Review Graph 只输出事实、证据和风险，不拥有 mutation、
validation、commit 或 integration authority。

## 4. 配置与启用

新增仓库静态配置 `code_review_enabled: bool = False`，沿用现有 coding repository configuration 的解析、
校验与 checkpoint 投影方式。默认关闭保证既有运行行为不变。配置只能由受信静态装配决定，不能根据用户文本、
关键词或模型输出动态开启。

启用后，每次成功 validation 都必须经过 review。关闭 integration 时也保留 review gate：批准后进入 applied terminal，
拒绝后进入 rejected terminal。

## 5. 输入、任务与输出契约

### 5.1 输入

`CodingReviewInput` 包含：

- `workspace_id`
- `generation`
- `base_commit`
- `snapshot_ref`
- `tree_digest`
- `diff_digest`
- `validation_evidence_digest`
- 固定有序 `review_tasks`

进入子图前必须创建 validation 后的最终快照。输入同时绑定当前 workspace identity、generation、base commit、
最终累计 diff、snapshot tree digest 和最近成功 validation evidence。任何缺失、过期、重复、漂移或 digest 不一致
都必须 fail closed。

### 5.2 固定任务 inventory

任务 inventory 精确为：

1. `correctness_regression`
2. `security_governance`
3. `tests_validation`

每项任务拥有独立 prompt，但共享同一个只读 snapshot Tool profile。任务 ID、顺序和数量由生产代码静态定义，
模型不能增删或改写。

### 5.3 Finding

`CodingReviewFinding` 至少包含：

- 稳定 `finding_id`
- `task_id`
- `severity`: `critical | high | medium | low`
- `category`
- `title`
- `explanation`
- 有界 `evidence`
- 可选、不可执行的 `remediation`

每条 evidence 必须引用策略允许的仓库相对路径、正整数行号和内容 digest。仅有自然语言断言而没有可验证证据的
finding 无效。字段长度、finding 数量、evidence 数量和最终 JSON 大小均设硬上限。

### 5.4 Reviewer result

`CodingReviewerResult` 包含任务 ID、`completed | unavailable`、findings、脱敏错误分类以及输入的
`snapshot_ref/tree_digest/diff_digest` 回显和 `output_digest`。Tool observation 与最终结构化结果都必须回显同一绑定。

终态 inventory 必须精确完整，不接受 unknown、duplicate 或缺失任务。`completed` 可以返回零 finding；
`unavailable` 不能伪装成 clean。

### 5.5 聚合报告

`CodingReviewReport.status` 为 `clean | findings | unavailable`。报告保留完整 canonical reviewer inventory、
规范化 findings、所有输入 digest 和 `report_digest`。

`join_review` 不调用 LLM，只做：

- schema 与 inventory 完整性校验；
- snapshot、tree、diff、validation evidence 和 output digest 校验；
- 按 `severity -> task_id -> path -> line -> finding_id` 确定性排序；
- 按 canonical evidence/semantic key 去重；
- 计算覆盖完整规范化报告的 `report_digest`。

任意 worker unavailable、非法输出或 inventory 不完整，报告状态均为 `unavailable`，不能降级为 clean。

## 6. Review Tool 与 Provider 边界

Review worker 只能读取 final review snapshot 中经过既有路径 policy、UTF-8、文件大小与累计预算校验的内容。
snapshot hash 可以覆盖原始树，但暴露给模型的内容必须是 policy-compliant project view。

工具集合只允许 review snapshot 的只读 list/read/search 类操作。禁止：

- workspace Path 或 Git common-dir 直接访问；
- shell、subprocess、网络和 Provider-native search；
- patch、validation、commit、merge、approval 或 durable task Tool；
- 读取 `.env`、凭据、Git 管理区、snapshot 管理元数据或越界 symlink。

Provider 使用现有 mock/real 双模式。pytest 只允许 mock；真实 Provider 只能由既有显式 operator 门禁启用。

## 7. HITL 与 resume 语义

`coding_review_decision` 是独立原生 interrupt，payload 至少绑定：

- `workspace_id`
- `generation`
- `base_commit`
- `snapshot_ref`
- `tree_digest`
- `diff_digest`
- `validation_evidence_digest`
- `report_digest`
- report status 与有界 findings 摘要

唯一合法决策为 `approve` 和 `reject`。`approve` 只表示用户接受当前审查结果，不代表模型宣称代码正确；
`reject` 立即进入 rejected terminal，不创建 commit、不准备 merge。

resume 必须重新解析 workspace，并校验 interrupt 与当前 checkpoint 的全部绑定。旧 generation、旧 diff、旧 validation、
旧 report 或 workspace 漂移全部 fail closed。LangGraph 节点可能重放，因此 snapshot 创建、worker result、join 和 approval
消费必须幂等、内容寻址或 checkpoint 安全。

## 8. 生命周期与清理

- 新 coding generation 开始时原子清除旧 review tasks、results、report、decision 和全部 digest。
- patch 或 validation evidence 变化后，旧 review 与 approval 立即失效。
- validation 再次成功后必须创建新 final snapshot 并重新审查。
- terminal state 清除 reviewer 临时 task/context，但保留最终报告、决策和绑定摘要供审计与流式输出。
- final review snapshot 复用 Stage 5B analysis snapshot 的受限物理存储与 snapshot-only reaper，不扩大 periodic owner。
- snapshot 删除或 TTL 到期不能使已经写入 checkpoint 的结构化报告失效，但任何尚未完成的 read/review 必须 fail closed。

## 9. 错误处理

- 权限、路径策略、digest、schema、identity、checkpoint 或 resume 错误：立即 fail closed，禁止进入 commit。
- 单个 worker 的稳定能力失败：形成 `unavailable` 结果，完整 join 后交给用户明确决策。
- cancel、GraphBubbleUp 与 permission 异常保持原异常语义，不包装成普通 unavailable。
- 输出过大、非 UTF-8、未知 task、重复 result、非法 evidence 或 digest 不一致：拒绝该结果并形成 unavailable 报告。
- Review Graph 不做自动 retry；由父运行的 durable/replay 语义保证节点级恢复，避免模型调用风暴。

## 10. 可观测性

流式事件公开稳定节点名：

- `prepare_review_snapshot`
- `review_workspace`
- `join_review`
- `coding_review_decision`

事件与 trace 只记录结构化状态、任务 ID、状态、耗时、计数和 digest，不记录完整仓库内容、原始 Provider 响应或凭据。
公开 `get_graph()` 拓扑必须展示 validation -> review -> review decision -> commit/terminal 的稳定顺序。

## 11. 测试策略

Core invariant：`LOOP-001` 扩展为“最终 validation 成功后，启用的独立只读 review 必须先于任何 commit/integration，
且 review approval 与最终 diff/validation/report digest 绑定”。只更新该 invariant 的文字和已有 topology/contract 保护；
不为 feature 机械新增永久 core 文件。

临时 TDD 位于 `tests/tdd/ai-coding-code-review/`，覆盖：

- Pydantic 边界、JSON 大小边界、digest 与 canonical ordering；
- 固定三个 Send worker、只读 Tool exposure、无 mutation/network/shell；
- final snapshot 与 Stage 5B initial snapshot 隔离；
- clean/findings/unavailable 三种报告；
- disabled passthrough 与 enabled review gate；
- approve/reject、stale resume、workspace drift、generation/diff/validation/report mismatch；
- replay 幂等、terminal 清理和公开 Graph topology。

临时 TDD 不提交、可由用户手动整目录删除。最终验证至少运行 Stage 5C TDD、相关 Stage 5B TDD、coding/core 回归、
authority validator，并确认 8089 hot reload 健康；不调用真实 Provider。

