# AI Coding Stage 5A：验证失败修复循环设计

## 1. 目标与范围

Stage 5A 在现有顺序 `AssistantCodingGraph` 内增加最多两轮的验证失败修复循环。只有 test、lint、build
固定命令正常启动并以非零 exit code 返回的 `verification_command_failed` 才可进入修复；timeout、OOM、
resource exceeded、sandbox/dependency/credential/artifact/cleanup、formatter 和协议错误直接以当前结构化结果终止。

修复仍复用唯一 thread-scoped coding workspace、现有 inspect/draft agent、patch validator、原生 LangGraph
interrupt、受信 apply、依赖与 artifact gates、validation service 和 integration service。不创建 Repair Runtime、
第二张可独立写入的 Graph、新 run、通用 shell 或自动放宽的资源/网络策略。

每轮模型只看到失败命令的有界 stdout/stderr 与结构化摘要。模型生成新的增量 repair patch；用户审批时看到从
冻结 base commit 到候选修复后的累计 diff。每轮 repair patch 都重新进入 digest-bound HITL，旧 approval 不复用。

## 2. 推荐流程

```text
run_validation
  ├─ passed -> create_commit / terminal
  ├─ eligible failure && repair_round < 2
  │    -> prepare_repair
  │    -> inspect_and_draft
  │    -> validate_patch
  │    -> patch_approval
  │    -> apply_patch
  │    -> plan_dependencies
  │    -> dependency_approval
  │    -> plan_credentials
  │    -> credential_approval
  │    -> plan_artifacts
  │    -> artifact_approval
  │    -> run_validation
  └─ ineligible failure / no progress / limit reached -> failed terminal
```

`run_validation` 后使用确定性 router 判断结果，不让 LLM 决定 failure 是否可修复。integration 只能在最终一次
validation 通过后进入。formatter 的既有单轮限制保持为整个 run 的全局限制，不因 repair round 重置。

## 3. State 与结构化契约

新增严格、冻结的 JSON-safe contract：

- `CodingRepairFailureEvidence`：`command_id`、`kind`、`exit_code`、`error_code`、`output_digest`、有界
  `stdout/stderr`、`truncated`；只允许单个 eligible failure。
- `CodingRepairAttempt`：`round`、触发 failure digest、repair patch digest、审批时 workspace diff digest、验证结果
  与终止原因；不保存完整 patch、完整累计 diff或宿主路径。
- state channel：`repair_round`、`repair_status`、`repair_failure_evidence`、`repair_history`、
  `repair_workspace_diff_digest`。

`repair_round` 初始为 0，`prepare_repair` 接受 eligible failure 后递增，最大为 2。进入新一轮时必须清除旧 proposal、
patch validation、patch approval、dependency/credential/artifact plan 与 approval 状态，但保留已经应用到 workspace 的
变更和有界 repair history。所有清理通过单个确定性 state update 完成，避免 resume 读取上一轮 pending authorization。

## 4. 模型输入与无进展检测

`prepare_repair` 生成一次性 repair context，作为最新真实用户请求后的临时 `HumanMessage` 仅用于本轮
inspect/draft 调用，不写入标准对话 messages。内容只包括：

- repair round 与剩余轮数；
- 失败 command ID、kind、exit code、稳定 error code、output digest 与 truncated；
- 已按现有 output budget 截断和脱敏的 stdout/stderr；
- 明确要求通过 coding read Tool 检查当前 workspace，并提交一个最小增量 patch。

模型仍通过现有 coding Tool 读取当前文件和 diff，不能从 repair context 获得宿主 path、container/network ID、
credential、artifact URL 或未投影的日志。若模型不产生 proposal、产生空 patch、重复历史 patch digest，或候选累计
workspace diff digest 与进入本轮前相同，则以 `coding_repair_no_progress` 终止，不继续消耗下一轮。

## 5. 累计审阅与审批绑定

repair patch 保持增量形式，以便现有 validator 在当前 workspace 上 dry-run 并由 apply service 原子应用；不重置
workspace，也不把从 base commit 生成的累计 patch重复应用到已修改文件。

repair approval payload 在现有字段之外增加：

- `repair_round`；
- `workspace_diff_digest`：进入本轮、尚未应用 repair patch 的当前累计 diff digest；
- `candidate_diff_digest`：当前累计 diff叠加 repair patch 后的候选累计 diff digest；
- 有界 `cumulative_diff_preview`。

approval decision 必须回显 `patch_digest`、`workspace_diff_digest` 和 `candidate_diff_digest`。resume 后重新解析
workspace、重跑 patch validation、重新计算两个 diff digest；任一漂移即 `coding_approval_mismatch`。apply 仍只应用
批准的增量 patch，不能调用模型或替换内容。首轮普通 patch 继续兼容现有 approval contract；扩展字段只在
`origin=repair` 时强制要求。

## 6. 重新执行治理 gates

每个 repair patch 应用后从 `plan_dependencies` 开始重新经过完整治理链：

- 未改变 lockfile、credential scope 或 artifact manifest 时，对应 plan 为空，不新增 interrupt；
- 改变受治理输入时生成新的 plan/request digest，并要求新的独立 HITL；
- 旧 dependency、credential、artifact approval 在 `prepare_repair` 时已清除，禁止跨轮复用；
- validation sandbox、network-none、artifact scanner、bundle TTL 和 cleanup 边界保持不变。

## 7. 错误与终态

以下情况直接终止且不进入 repair：

- timeout、OOM、resource/disk/output/process limit；
- sandbox image/protocol/start/output/cleanup 错误；
- dependency、credential、artifact fetch/scan/install/cleanup 错误；
- formatter command failure或 formatter round 超限；
- 没有唯一 eligible failure；
- repair round 已达到 2。

达到上限时保留最后一次 `CodingVerificationResult`，附加 `repair_status=exhausted` 和有界 repair history。用户拒绝
任一 repair patch 时沿用现有 rejected terminal，不自动尝试另一 patch。Graph cancel、interrupt、resume 与 checkpoint
仍由 Agent Server 原生拥有。

## 8. 测试与验收

临时 RED/GREEN 使用 `tests/tdd/ai-coding-repair-loop/`，可由用户手动整目录删除，不自动晋升 core。覆盖：

1. 仅普通 test/lint/build 非零退出可进入 repair；基础设施与资源错误不可进入。
2. repair context 只包含单个失败命令的有界、脱敏 evidence。
3. 新一轮清除旧 proposal 与全部治理 approval，但保留当前 workspace 和 repair history。
4. repair HITL 同时绑定增量 patch、当前 workspace diff 和候选累计 diff；resume 漂移失败关闭。
5. 每个 repair apply 后重新经过 dependency、credential、artifact gates。
6. 第一轮修复成功后才能 integration；两轮失败后以 exhausted 终止。
7. 空 patch、重复 patch 或累计 diff不变以 no-progress 终止。
8. reject、cancel、checkpoint/resume 不复用上一轮授权。

本阶段改变 `LOOP-001` 中生产 CodingGraph 的稳定控制流：更新既有 invariant 描述和最小 runtime lifecycle 断言，
不新建 core 文件。全部 pytest 使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`、offline，不调用真实 Provider 或网络。

## 9. 非目标

Stage 5A 不包含只读并行分析、独立 code review graph、跨 run 长任务恢复、行为评测、自动 push/PR、冲突自动
修复、基础设施故障自愈、任意命令、validation 网络访问或免审批的自动 patch。上述能力分别留给后续 Stage 5
子阶段。
