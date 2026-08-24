# AI Coding Stage 5D：Review 驱动的受控修复闭环设计

## 1. 目标

Stage 5D 在 Stage 5C 独立只读 Code Review Graph 之上，为 `coding_review_decision` 增加受控
`respond` 决策。用户可根据结构化 findings 提交修复指导，父 `AssistantCodingGraph` 将指导投影给既有
inspect/draft mutation lane；新 patch 必须重新经过 patch approval、deterministic validation、final review 和
review decision。

Review Graph 始终只读，不生成或应用 patch，不获得 mutation、validation、commit、merge 或 approval authority。

## 2. 非目标

- 不让 reviewer 自动修改代码或自动触发修复。
- 不根据 severity 自动批准、拒绝或修复。
- 不新增第二套 coding agent、Agent Server run 或 integration runtime。
- 不允许绕过既有 proposal validation、patch HITL、validation repair 或 merge HITL。
- 不为 unavailable reviewer failure 引入自动 retry。
- 不扩大 snapshot periodic reaper、Git worktree 或 admin lifecycle owner。

## 3. 拓扑

```text
run_code_review
    -> coding_review_decision
         ├─ approve -> create_commit / applied terminal
         ├─ reject  -> rejected terminal
         └─ respond (findings only)
              -> consume_review_repair_budget
              -> inspect_and_draft
              -> validate_proposal
              -> patch_approval
              -> apply_patch
              -> run_validation
              -> prepare_review_snapshot
              -> run_code_review
              -> coding_review_decision
```

`respond` 不直接跳到 apply 或 validation。它只建立一次新的 inspect/draft 输入，并使上一轮 patch、validation、
review 与 integration authorization 全部失效。

## 4. 决策契约

`coding_review_decision` 合法决策扩展为：

- `approve`
- `reject`
- `respond`

`respond` payload 必须包含非空、规范化、长度有界的 `response`。只有当前 report status 为 `findings` 时合法；
`clean` 和 `unavailable` 只能 approve/reject。未知字段、空白文本、超限文本、旧 interrupt 或重复消费均 fail closed。

decision context 继续完整绑定 workspace、generation、base commit、validation snapshot、tree/diff、validation evidence、
review snapshot、report digest 和 snapshot schema version。

## 5. 修复上下文

`CodingReviewRepairContext` 是冻结 Pydantic model，包含：

- 当前 `review_repair_attempt`
- `report_digest`
- `validation_evidence_digest`
- `workspace_diff_digest`
- 规范化用户 `response`
- 有界、规范化 findings 摘要

findings 摘要最多 12 条，只包含 finding ID、task ID、severity、category、title、首个 path/line 和 remediation；
不复制完整 Provider 响应、完整仓库内容或未验证 evidence。

inspect agent 的系统边界不变。父图将 repair context 作为结构化、一次性 context 投影到当前 inspect invocation；
模型仍只能使用既有 read Tool 和 `coding_propose_patch`。

## 6. 独立预算

`MAX_CODING_REVIEW_REPAIR_ATTEMPTS = 2`。预算与 Stage 5A validation repair budget 相互独立。

每个合法 `respond` 必须先进入独立 `consume_review_repair_budget` checkpoint node：

- 当前计数 0 或 1：原子增加到 1 或 2，再允许调用 inspect agent。
- 当前计数已经为 2：不调用模型、不创建 patch，进入 `review_repair_exhausted` terminal。
- replay/resume 不能重复消费同一 respond。

预算只在新的 coding generation 开始时重置；同一 generation 内 patch rejection、validation repair 或 review re-review
均不重置。

## 7. 失效与状态清理

接受 `respond` 后必须在同一个原子 state update 中：

- 保存新的 `review_repair_context` 和审计 history；
- 清除上一轮 review decision/approval、active report、review tasks/results/context；
- 清除上一轮 validation evidence 与 validation snapshot binding；
- 清除 integration required、commit、merge approval/context/result；
- 清除旧 patch proposal、patch approval/context、apply/validation terminal 状态；
- 保留 workspace identity、generation、base commit、用户原始 coding request 和 review repair count。

上一轮 final validation/review snapshot lease 在状态不再需要后必须确定性、幂等释放；释放失败记录 owner-reaper 状态，
不得掩盖 decision 或 resume 的原始错误。

新 proposal 形成后，repair context 标记 consumed，后续 formatter/validation repair 不重复投影旧指导。新 validation 成功后
必须创建新 final snapshot、新 report digest 和新 decision interrupt。

## 8. Resume 与 fail-closed

所有 START、interrupt resume 和 non-terminal checkpoint 继续执行完整 workspace resolution。以下情况拒绝：

- decision context 与 checkpoint 任一 digest/version 不一致；
- report 不是当前完整 `findings` report；
- response 已消费或 attempt token 不一致；
- patch/diff、validation evidence 或 workspace 在 interrupt 后漂移；
- orphaned repair context/history/count 组合；
- count 超出 `0..2` 或 history 不连续；
- active review、patch approval、merge approval 等互斥 channel 同时存在。

permission、identity、snapshot、schema、path policy 和 digest 错误保持 fail closed；cancel/GraphBubbleUp 保持原异常。

## 9. 审计与终态

`CodingReviewRepairAttempt` history 每轮记录 attempt、旧 report/diff/validation digest、用户 response digest、findings IDs、
created_at 和 outcome。history 最多两项且顺序连续。

最终 terminal 保留 history、最终 review report 和最终 decision 摘要；清除 active repair context、临时 reviewer state、
patch/merge approval context 和不再需要的 snapshot handle。

流式事件公开稳定节点 `consume_review_repair_budget`，并保持 review -> decision -> repair -> proposal -> validation -> review
的公开 topology。

## 10. 配置与安全

Stage 5D 不新增用户可控开关。功能仅在 repository-static `code_review_enabled=true` 时可达，最大轮数固定为 2。

repair context 来自用户 decision payload 与已验证 report，不进入 Tool exposure。reviewer 继续只能使用 snapshot-bound read
Tools，`provider_search_profile=none`；所有 mutation 继续位于父图单线 lane。

## 11. 测试

临时 TDD 使用 `tests/tdd/ai-coding-review-repair/`，不提交、可由用户手动整目录删除。覆盖：

- respond schema、findings-only、文本/JSON 精确边界；
- 第 1/2 次先 checkpoint 消费预算，第 3 次不调用模型；
- repair context 一次性投影与 findings 摘要边界；
- 旧 patch/validation/review/integration state 原子失效；
- snapshot lease 正常/异常/重复释放；
- patch approval、validation 和 re-review 全链强制执行；
- stale/replayed decision、workspace/digest/schema drift、orphaned channel fail closed；
- approve/reject/default-off/Stage 5C 行为不回归；
- public Graph topology 与 terminal audit history。

`LOOP-001` 扩展为 review respond 只能通过有界、checkpointed repair loop 返回 mutation lane；是否修改已有 core 测试由
`tests/README.md` 与现有 invariant owner 决定，不新增机械永久测试文件。

