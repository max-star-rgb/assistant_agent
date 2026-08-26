# AI Coding Stage 5E：原生行为评测基线设计

## 1. 目标

Stage 5E 为已经完成的 CodingGraph 建立可重复、可审计的端到端行为评测基线。评测直接通过生产
LangGraph Agent Server 驱动 `AssistantRootGraph -> AssistantCodingGraph`，覆盖真实
thread/run/checkpoint/interrupt/resume 生命周期，并用临时 Git fixture 与 held-out deterministic grader 判断结果。

本阶段回答“当前 coding agent 在受控仓库中能否正确完成任务”，不再以 node-level mock、旧 Runtime facade
或只检查最终自然语言回复替代能力证据。

## 2. 为什么现在做

Stage 5A 至 5D 已完成验证失败修复、只读并行分析、独立 code review 和 review repair。继续增加长任务恢复、
远程 PR 或更多自动化前，需要先获得以下证据：

- 能否在小型真实仓库中生成最小且正确的 patch；
- 能否经过 patch approval、validation、review、review decision、controlled merge；
- 是否修改了任务范围外文件；
- 是否通过 held-out tests，而不是只复述模型自报结果；
- 失败发生在哪个原生 checkpoint 和治理边界。

评测结果将作为 Stage 5F 长任务恢复的输入，不反向修改本阶段的 case 或 grader 来迁就当前模型。

## 3. 权威边界

- `evals/README.md` 继续拥有 system eval、真实 Provider/operator 门禁与 evidence 规则。
- `docs/runtime-event-stream-architecture.md` 继续拥有生产 CodingGraph 行为；Stage 5E 不修改其决策语义。
- `tests/README.md` 继续拥有临时 TDD；本阶段测试位于 `tests/tdd/ai-coding-behavior-eval/`。
- `src/assistant_agent/evaluation/` 只提供 evaluation contract/driver，不拥有产品状态机。
- `evals/system/ai_coding_behavior/` 保存正式 runner、受信 fixture 定义和 grader。

## 4. 明确不做

- 不恢复旧 Runtime Regression、Release Review runner 或旧 `AgentState` facade。
- 不让 grader 调用第二个 LLM，不以 LLM-as-judge 作为首个可信基线。
- 不在 mock 模式输出“能力通过”；mock/offline 只验证 contract、dry-run 和安全门禁。
- 不读取任意用户仓库路径，不接受任意 shell argv、任意测试命令或任意 approval payload。
- 不访问网络，不审批 dependency、credential 或 artifact ingress。
- 不 push、不创建 PR、不写远程仓库。
- 不把 Provider 原始响应、完整 prompt、源码正文、secret 或 `.env` 写入 evidence。
- 不修改生产 CodingGraph 来让评测通过。

## 5. 总体架构

```text
tracked case manifest
        |
        v
trusted fixture builder -> temporary Git repository
        |                         |
        |                         +-> server-owned coding repository allowlist
        v
Agent Server client -> thread -> coding run -> native interrupts/resume
        |                                      |
        |                                      +-> patch/review/merge approval policy
        v
terminal checkpoint + merged temporary repository
        |
        v
deterministic graders -> redacted result.json
```

正式 runner 连接现有单实例 `8089`，不在同一目录启动第二套 Server。`--dry-run` 不连接 Server、不创建 Git
仓库、不读取真实 Provider 配置值，只校验 case/schema 并输出计划调用和安全边界。

## 6. Case contract

`CodingBehaviorCase` 使用严格、版本化 Pydantic schema：

- `schema_version=1`；
- `case_id`、`title`、`request`；
- `fixture_id`：只引用代码中受信静态 fixture builder；
- `expected_changed_paths` 与 `allowed_changed_paths`；
- `forbidden_changed_paths`；
- `grader_ids`：只引用受信静态 grader catalog；
- `max_runtime_seconds`；
- `required_interrupts`，首版只允许 `patch_approval`、`coding_review_decision`、`merge_approval`；
- `tags`。

manifest 不包含宿主路径、命令 argv、环境变量、approval payload、Provider 配置或测试源码正文。所有 tuple
排序、路径规范化、重复项和集合关系在运行前 fail closed。

首个基线包含四类 fixture：

1. 单文件逻辑 bug 修复；
2. 多文件接口一致性修复；
3. 需要增加回归测试的行为修复；
4. 包含诱导性无关文件的 scope discipline 场景。

## 7. 原生 Agent Server driver

`CodingBehaviorAgentServerDriver` 只通过公开 `langgraph_sdk`：

1. 使用专用 evaluation identity 创建绑定当前 graph ID 的 thread；
2. 提交 `execution_mode=coding`、固定 `coding_repo_id` 和 case request；
3. 读取原生 interrupt/checkpoint；
4. 由严格 `FixtureApprovalPolicy` 生成 resume 值；
5. 直到 terminal 或 case deadline；
6. 返回有界、结构化 transition evidence。

driver 不复制 CodingGraph 状态机。它只认识允许自动响应的 interrupt 类型和最大次数；未知、中间重复、缺字段、
binding 不一致或超预算 interrupt 全部停止评测。首版策略：

- patch approval：approve；
- code review findings：approve，不自动触发 repair，repair 另由专项 case 覆盖；
- merge approval：approve；
- dependency/credential/artifact/未知 interrupt：deny 并使 case 失败。

自动审批只允许 runner 自己创建的临时 fixture repo。repo ID、source root、target branch、base commit 与 thread identity
在 driver 创建时冻结；任一漂移停止，不把 policy 暴露为生产默认行为。

## 8. Fixture 与 mutation 边界

每个 case 在 `.data/evals/system/ai_coding_behavior/work/` 下创建独立临时根，fixture builder 写入固定小文件并初始化
Git。runner 生成只对该临时 repo 生效的 coding repository 配置：

- `integration_enabled=true`；
- `code_review_enabled=true`；
- validation command 只引用固定 runner-owned command ID；
- analysis 可按 case 静态配置；
- dependency、credential、artifact profile 全部为空；
- target branch 为 fixture 的本地 `main`；
- 不配置 remote。

成功或失败后都执行 best-effort cleanup。清理失败写入 `cleanup_pending=true` 并返回失败，不递归删除 allowlist 外路径。

## 9. Deterministic graders

grader 不读取模型自报结论，只消费 terminal checkpoint、Git object/working tree 与 fixture-owned command result：

- `terminal_status`：必须到达允许的 coding terminal；
- `held_out_tests`：在合并后的临时仓库运行固定、代码内声明的离线命令；
- `changed_path_scope`：actual paths 必须是 `allowed_changed_paths` 子集，并满足 case 的 expected relation；
- `forbidden_paths_unchanged`：比较 base 与 target tree digest；
- `native_lifecycle`：要求 case 声明的 interrupt 按顺序出现且无未知 interrupt；
- `integration_binding`：最终 commit/merge binding 与被 review/validation 的 tree 一致；
- `bounded_execution`：deadline、interrupt 次数和结果大小均未超限。

任何 grader exception 都形成结构化 failed check，不被 runner 吞成通过。case 只有全部 required check 通过才 passed；
suite 只有全部 required case 通过才 passed。

## 10. Evidence 与隐私

每次真实运行写入 `.data/evals/system/ai_coding_behavior/<timestamp>_<suite-id>/result.json`。只保存：

- schema/version、case ID、fixture ID、Provider/model 的非 secret 标识；
- thread/run ID 的 session-local digest，不保存可复用认证信息；
- terminal status/error code；
- interrupt kind、顺序、次数和 checkpoint digest；
- base/final commit digest、changed path 名称和 grader 结果；
- latency、cleanup status 和 aggregate pass/fail。

不保存源码内容、patch 正文、完整消息、prompt、Provider 原始响应、stdout/stderr 全文、环境变量或 key。grader 输出只保存
退出码、截断摘要 digest 和有界错误分类。

## 11. CLI 与真实运行门禁

稳定入口：`scripts/run_system_ai_coding_behavior_eval.py`。

默认 `--dry-run`。真实运行同时要求：

- `MULTIMODAL_AGENT_PROVIDER_MODE=real`；
- chat Provider 配置完整；
- `--allow-real-provider`；
- `--allow-local-git-mutation`；
- `--server http://127.0.0.1:8089`；
- operator 提供精确 suite ID；
- Server 当前 coding allowlist 明确包含 runner 生成的 opaque repo ID；若 hot reload 后配置未生效则 fail closed。

本阶段实现和默认验收不执行真实模式。后续真实运行必须由 operator 再次明确授权，并在最终报告列出 Provider 调用范围、
case 数量、Git mutation 范围和 cleanup 结果。

## 12. 失败分类

稳定错误至少包括：

- `coding_eval_configuration_error`；
- `coding_eval_case_invalid`；
- `coding_eval_server_unavailable`；
- `coding_eval_repository_not_bound`；
- `coding_eval_unknown_interrupt`；
- `coding_eval_interrupt_budget_exceeded`；
- `coding_eval_deadline_exceeded`；
- `coding_eval_terminal_mismatch`；
- `coding_eval_grader_failed`；
- `coding_eval_cleanup_pending`。

异常、cancel 和 SDK transport error 保留 cause 分类；不得改写成模型质量失败。

## 13. 验收标准

- dry-run 在 mock/offline 下验证全部 case、catalog、门禁和 planned calls，且不连接 Server/Provider、不创建 repo；
- driver 的 scripted SDK TDD 覆盖 thread/run/interrupt/resume/terminal、未知 interrupt、预算、deadline 和身份绑定；
- fixture/grader TDD 使用真实本地 Git，证明 held-out、path scope、forbidden path 和 merge binding；
- artifact TDD 证明不落盘源码、patch、消息、secret 或原始 Provider response；
- 正式入口只有 real mode + 双授权开关才可能发起调用；
- `evals/README.md` 与 `scripts/README.md` 同步入口和安全边界；
- core invariant unchanged；不修改生产 CodingGraph；
- authority validator、compileall、Stage 5D covering tests 与 `8089 /ok` 通过。

## 14. 后续阶段

Stage 5F 根据真实 Stage 5E evidence 实现长任务恢复与 no-progress 管理。Stage 5E 不预设恢复算法，也不把评测失败自动
转成 runtime retry。远程 push/PR 继续作为更晚的独立危险能力。

## 15. 实施裁决：静态 allowlist 的两阶段绑定

Agent Server 在进程装配时从环境冻结 coding repository allowlist，不能让同一 runner 在创建随机 fixture 后动态修改。
因此真实 Stage 5E 采用单进程两阶段：runner 持有全部 fixture capability，向交互式 operator 显示一次性精确 repository
配置和随机 reload nonce；operator 只重启现有回环 `8089`，精确输入 `RELOADED <nonce>` 后 runner 才读取实际 Server
composition attestation。operator 随后精确 ACK reload nonce、Server boot nonce 与 registry digest，runner 才创建 thread。
attestation canonical digest 冻结进 evaluation checkpoint，driver 在每个 interrupt/terminal 校验，同一 case 前后还要重新采样
完全相同的 attestation。非 TTY、ACK replay、未精确确认、配置未生效或绑定漂移全部 fail closed；runner 不启动第二个
Server，不动态修改生产 Graph，也不把宿主路径、raw boot nonce 或 raw ephemeral repo ID 写入 result artifact。

## 16. 最终安全收口裁决

- execution attestation 仅由 Agent Server auth hook 对 identity/repository/case 签发并冻结。客户端单独伪造
  `entry_profile=evaluation` 不会启用 attestation；普通 coding state 与 worker `Send` 的 digest 为 `None`，跨
  reload 不失败。已冻结 evaluation checkpoint 缺失或 mismatch 时所有 node 在副作用/Provider 调用前失败。
- fixture 内容清理成功后的诚实状态为 `released_with_bounded_sentinel`，不宣称目录已删除。固定 work root 内使用
  fd-anchored、schema/owner/root-inode 绑定的 marker registry，跨 suite 最多保留 64 个 store；达到上限在任何
  repository mutation 前 fail closed 并要求 operator cleanup。store 支持 context manager/`close()` 并关闭所有 fd。
- fixture、snapshot、artifact/output root 都从可信 repository root dirfd 逐层 `mkdirat/openat(O_NOFOLLOW)`。
  artifact temp/write/fsync/rename 与最终 inode 复核均相对冻结 dirfd，不提供绕过 capability 的任意 public path。
