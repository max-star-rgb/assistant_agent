# AI Coding Stage 5F：长任务恢复与无进展管理设计

## 1. 目标

Stage 5F 为生产 `AssistantCodingGraph` 增加本地、原生、可检查点恢复的 inspect epoch。在首次
`inspect_and_draft` 因 Tool 或 Model 调用预算耗尽而无法形成 proposal 时，Graph 不再让 middleware 异常直接逃逸，
而是保存有界进度证据，在同一 thread/run/checkpoint 生命周期内最多恢复两次。恢复没有产生新证据时以确定性
`coding_inspect_no_progress` 终止。

本阶段只解决长任务控制流和恢复治理，不承诺提高具体模型的修复质量。

## 2. Stage 5E evidence

2026-08-26 的 `baseline-v1` 真实运行覆盖四个临时 Git fixture。四个 case 都进入生产
`AssistantRootGraph -> AssistantCodingGraph`，其中服务日志明确出现
`ToolCallLimitExceededError: run limit exceeded (13/12 calls)`。当前异常会让原生 run 失败，评测侧最终只能得到
`coding_eval_repository_not_bound` 或 `coding_eval_unknown_run_outcome`，无法形成 proposal、approval 或 grader evidence。

该 evidence 只用于确定 Stage 5F 的恢复入口。Stage 5F 不修改 Stage 5E manifest、case、grader 或 error projection，
也不把当前 baseline 改写成通过。

## 3. 权威与边界

- `docs/runtime-event-stream-architecture.md` 继续拥有 CodingGraph 拓扑、state、checkpoint 和恢复语义。
- `docs/tool-calling-architecture.md` 继续拥有标准 `BaseTool -> ToolNode`、ToolRuntime 和 Tool exposure。
- `docs/agent-server-architecture.md` 继续拥有 thread/run/checkpoint/cancel/interrupt/resume 生命周期。
- `tests/README.md` 继续拥有 core/TDD 归属；Stage 5F 临时测试放在
  `tests/tdd/ai-coding-long-task-recovery/`。
- Stage 5F 不建立 scheduler、background worker、第二套 Runtime 或 runner retry。

## 4. 非目标

- 不提高或删除现有 Tool/Model 单 epoch 安全预算。
- 不自动重试 Provider、permission、identity、sandbox、workspace、validation、cancel 或未知异常。
- 不处理 Stage 5E 暴露的评测错误分类和模型业务质量。
- 不修改 patch approval、validation repair、review repair、commit、merge 或 snapshot 生命周期。
- 不启用 Provider-native code execution、远程 push、PR 或代码托管凭据。
- 不把 inspect 原始 transcript、源码、Tool result、prompt 或 Provider response 写入父 Graph state。

## 5. 方案裁决

采用“graceful budget termination + checkpointed inspect epoch”。

不采用以下方案：

1. 单纯把 `12` 提高到更大值。该方案只延迟失控，不提供恢复证据和 no-progress 终态。
2. 由 evaluation runner 或 Agent Server 创建新 run 重试。该方案复制产品生命周期，并可能绕过原 checkpoint 的身份、
   workspace 和审批绑定。
3. 保存完整 inspect transcript 后 replay。该方案扩大 checkpoint、泄露源码和 Provider 内容，并把 create-agent 私有状态
   变成产品协议。

## 6. 单 epoch 终止语义

生产 inspect agent 保留现有 `model_call_limit` 和 `tool_call_limit` 数值：

- `ToolCallLimitMiddleware(..., exit_behavior="continue")`：超额 Tool call 变成标准 error `ToolMessage`，允许模型在
  剩余 Model 预算内收敛为 proposal；不执行超额 Tool。
- `ModelCallLimitMiddleware(..., exit_behavior="end")`：Model 预算耗尽时正常结束 create-agent invocation，并返回
  其标准 messages，而不是向外抛出 `ModelCallLimitExceededError`。

analysis 和 final review agent 不改变现有 `exit_behavior="error"`；Stage 5F 只治理顺序 mutation lane 前的 primary inspect。

如果 graceful termination 后已经形成合法 proposal，继续既有 `validate_proposal`。只有没有 proposal，且 transcript
中存在 canonical budget-exhaustion sentinel 时，才进入 inspect recovery。普通 schema、Tool、Provider 或未知错误继续
沿现有 fail-closed 语义结束，不得伪装成预算耗尽。

## 7. 有界进度证据

新增严格 Pydantic contract：

```text
CodingInspectCallEvidence
  tool_name: str
  arguments_digest: hex64
  result_digest: hex64
  relative_paths: tuple[str, ...]

CodingInspectProgress
  schema_version: Literal[1]
  epoch: int                 # 1..3
  reason: tool_budget_exhausted | model_budget_exhausted
  base_commit: hex40..64
  workspace_diff_digest: hex64
  calls: tuple[CodingInspectCallEvidence, ...]
  progress_digest: hex64

CodingInspectRecoveryAttempt
  schema_version: Literal[1]
  epoch: int
  previous_progress_digest: hex64 | None
  progress: CodingInspectProgress
  outcome: pending | retrying | completed | no_progress | exhausted | terminal
```

提取规则：

- 只接受当前静态 read Tool inventory 中的标准 AI tool call 与对应 `ToolMessage`。
- `arguments_digest` 和 `result_digest` 基于 canonical JSON/UTF-8 计算；不保存原始参数或结果。
- `relative_paths` 只从 schema 已声明的 repository-relative path 字段提取，执行既有 canonical path 校验，单次最多
  32 个、每个最多 240 字符；其他参数不投影。
- call evidence 按 `(tool_name, arguments_digest, result_digest, relative_paths)` 去重排序，最多 64 项。
- `progress_digest` 绑定 schema version、reason、base commit、workspace diff 和 canonical calls，但不包含时间、epoch 或
  run/thread 原始标识。

父 checkpoint 不保存 inspect 的 Human/AI/Tool transcript。恢复 prompt 只投影已检查的 Tool 名和相对路径，以及固定的
“避免重复读取并尽快形成 proposal”规则；digest 和内部错误码不作为自然语言任务内容。

## 8. State 与拓扑

`CodingState` 新增：

```text
inspect_epoch: int
inspect_recovery_status: None | pending | retrying | completed | no_progress | exhausted
inspect_progress: CodingInspectProgress | None
inspect_recovery_history: tuple[CodingInspectRecoveryAttempt, ...]
inspect_recovery_context_consumed: bool
```

新增两个节点：

```text
inspect_and_draft
  -> validate_proposal                 # 合法 proposal
  -> evaluate_inspect_progress         # canonical budget exhaustion
  -> summarize                         # 其他 terminal

evaluate_inspect_progress
  -> consume_inspect_recovery_context  # 有新进展且预算可用
  -> summarize                         # no_progress / exhausted / invalid

consume_inspect_recovery_context
  -> inspect_and_draft
```

`evaluate_inspect_progress` 是唯一增加 epoch 和消费恢复预算的节点。首次 invocation 为 epoch 1；最多进入 epoch 3，等价于
最多两次恢复。`consume_inspect_recovery_context` 只在下一次 model call 构造临时 context，并原子设置
`inspect_recovery_context_consumed=True`；不得写入父 `messages`。

## 9. No-progress 与预算

以下任一条件直接形成 `coding_inspect_no_progress`：

- 当前 canonical call evidence 是上一 epoch evidence 的子集；
- 当前 `progress_digest` 与任一先前 retrying attempt 相同；
- workspace/base binding 未变且没有新增 `(tool_name, arguments_digest, result_digest)`；
- recovery context 已标记 consumed，但下一 epoch 又以相同预算原因结束且没有新相对路径。

epoch 3 仍没有 proposal但存在新进展时，形成 `coding_inspect_recovery_exhausted`。两种终态都保留 canonical history，
释放不再需要的 analysis/review snapshot，并且不进入 patch approval、validation 或 integration。

## 10. Checkpoint、resume 与清理

- pending/retrying checkpoint 必须重新验证 identity、thread、repo、workspace、base commit 和 execution attestation。
- resume 只继续当前 epoch，不重跑已完成 analysis，不创建第二个 workspace。
- 新 coding cycle 原子清空全部 inspect recovery channel。
- proposal 通过 `validate_proposal` 后将最新 attempt 标为 `completed`，清除临时 progress/context，只保留有界 history 供
  terminal audit。
- terminal、cancel 和 fail-closed 路径调用既有 snapshot/workspace cleanup；清理必须幂等。
- server reload 后 checkpoint contract 不匹配时 fail closed，不做隐式 migration。Stage 5F 不改变 graph ID。

## 11. 错误语义

新增生产错误码：

- `coding_inspect_no_progress`
- `coding_inspect_recovery_exhausted`
- `coding_inspect_recovery_binding_mismatch`

只有前两个是正常有界终态。binding/schema/path/digest/history 不一致使用
`coding_inspect_recovery_binding_mismatch` 并 fail closed。Provider、permission、cancel、Tool execution 和未知异常保留
原错误，不进入恢复。

## 12. 测试策略

临时 RED/GREEN 位于 `tests/tdd/ai-coding-long-task-recovery/`，至少覆盖：

- Tool budget graceful termination 不执行第 13 个 Tool，并能在剩余 Model 预算内形成 proposal；
- Model budget graceful termination 返回可提取 messages；
- epoch 1 有新增 read evidence时进入 epoch 2；
- 重复 evidence 在下一 epoch 形成 `coding_inspect_no_progress`；
- 持续有新 evidence 只允许到 epoch 3，随后 `coding_inspect_recovery_exhausted`；
- progress contract 拒绝绝对路径、symlink 语义、未知 Tool、extra field、oversize inventory 和非法 digest；
- resume 不重跑 analysis，不创建新 workspace，不跳过 approval/validation/review/integration；
- Provider、permission、cancel、sandbox 和未知异常不进入 recovery；
- terminal/cancel/reload mismatch 清理与 fail-closed 行为。

本阶段改变公开 CodingGraph 拓扑，因此更新 `LOOP-001` 描述，并在现有
`tests/core/integration/test_runtime_lifecycle.py` 只增加拓扑级断言。具体 epoch、digest、错误码和 prompt 行为继续留在临时
Stage 5F TDD，不进入 core。

## 13. 文档与验证

实现完成后同步：

- `docs/runtime-event-stream-architecture.md`
- `tests/core/INVARIANTS.md`
- 必要时 `evals/README.md` 仅说明 Stage 5E evidence 与 Stage 5F 的消费关系，不修改 baseline contract
- `docs/authority.toml` 已有 owner 路由，除非新增 source path，否则不改 manifest

最小验证：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/tdd/ai-coding-long-task-recovery
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/tdd/ai-coding-behavior-eval
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/core/integration/test_runtime_lifecycle.py
python scripts/check_documentation_authority.py --repo-root .
python -m compileall -q src/assistant_agent
```

本阶段开发与 pytest 全部 mock/offline，不重新运行真实 Provider baseline。真实 Stage 5E rerun 留到用户完成整体体验和业务
修复后单独授权。

## 14. 退出条件

- primary inspect 的 Tool/Model 预算耗尽不再以 middleware exception 逃逸；
- 恢复完全位于同一原生 CodingGraph/checkpoint，最多两个 recovery epoch；
- 相同 evidence 确定性终止为 no-progress；
- mutation、approval、validation、review 和 integration 安全边界不变；
- checkpoint 不保存源码、原始 transcript 或 Provider response；
- Stage 5F TDD、Stage 5E covering、定向 core、authority validator 和 compileall 全部通过；
- 不启用远程 Git 能力或 Provider-native code execution。

## 15. 后续路线

Stage 5F 完成后，本地安全编码核心链进入体验冻结期。下一步不是自动开发新阶段，而是由用户进行真实项目体验并集中修复
业务问题。只有体验结论确认本地链稳定后，才单独设计可选 Stage 5G：远程 push、PR 和代码托管协作；Stage 5G 必须拥有
新的凭据、目标仓库、branch protection、审批、幂等和审计设计，不能复用本阶段授权。
