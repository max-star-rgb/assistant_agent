# Evaluation Authority

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | System eval 与原生 Graph 评测 target 的当前权威 |
| Owns | 真实能力专项验证、operator 门禁、system artifact、原生 Graph evaluation target |
| Does not own | 默认 pytest、生产 Graph 实现、LangSmith Dataset/Experiment 的未来重建方案 |
| 源码与 schema 入口 | `evals/system/`、`src/assistant_agent/evaluation/` |
| 验证入口 | `docs/authority.toml` 中 `system-eval.verification` |
| 相邻 authority | `tests/README.md`、`docs/runtime-event-stream-architecture.md`、`docs/observability-harness.md` |

## 当前验证分层

当前只保留 System eval：真实能力评审在 operator 明确授权下验证 Provider、Tool、Memory 或媒体能力，并把有限、
脱敏的结果写入 `.data/evals/system/`。`evals/system/tools/` 另保留离线 Tool 执行冒烟：每个当前注册 Tool 一个
可在 PyCharm 直接运行的固定输入脚本，只判断标准 `ToolNode` 调用是否成功返回结果，不验证候选数量、排序或具体
业务内容；`run_all.py` 一键运行该目录及子目录的全部非 helper 冒烟脚本。默认 pytest 与临时 TDD 的边界仍由
`tests/README.md` 管理。

旧 Runtime Regression、Workflow Regression 与 Release Review runner 已随通用旧 Graph Runtime 删除。它们的
evidence、fixture backend、catalog 与终态合同绑定旧 `AgentState`，不能通过兼容 facade 投影到新图。仓库提供
`NativeGraphEvaluationTarget` 仍是直接调用生产 `AssistantRootGraph` 的基元。Stage 5E 另建立
`ai_coding_behavior` 原生行为基线：它只通过现有 Agent Server 的公开 thread/run/checkpoint/interrupt/resume
生命周期驱动生产 CodingGraph，以受信临时 Git fixture 和 deterministic grader 形成证据；评测层不拥有或复制
产品状态机，也不是通用上线前 Release Review 门禁。

## AI Coding behavior baseline

稳定入口为 `scripts/run_system_ai_coding_behavior_eval.py`，固定 suite 为 `baseline-v1`。默认 `--dry-run` 只读取
tracked、无 symlink、大小受限的 manifest 并验证 schema/catalog，不连接 Server、Provider，不创建 Git fixture。
真实模式固定连接 `http://127.0.0.1:8089`，要求 real mode、digest-pinned sandbox image、双授权开关，以及 operator
对一次 reload nonce 和 Server execution attestation 的精确 ACK。Server attestation 绑定 graph/provider/model、
boot incarnation、coding registry 和 repository config digest；同一 digest 冻结进 evaluation checkpoint，并在
每次 case 前后复核。redirect、DNS alias、未知 interrupt、ACK replay、attestation 漂移和 cleanup debt 均 fail closed。

结果 artifact 只保存严格 schema 的脱敏投影和 digest，不保存源码、patch、消息、prompt、Provider 原始响应、
宿主路径、secret-like key/value 或可复用身份。`provider_native_code_execution=disabled` 是 v1 固定 execution
profile；百炼原生 code execution 不算 held-out repository validation，也不会被 runner 静默启用。

## 真实运行安全

真实 System eval 必须同时满足：

- `MULTIMODAL_AGENT_PROVIDER_MODE=real`；
- 对应 Provider/Tool 配置完整；
- runner 的 real/staging 显式开关；
- operator 明确确认调用范围与副作用；
- 使用本机未跟踪配置，不提交 key、原始响应、真实用户数据或远端 artifact。

普通 pytest、`--help`、schema 校验与本任务验证保持 mock/offline。不得检测到 key 后自动启用真实调用，也不得
用 mock fallback 冒充真实能力验证。

## 维护与验证

稳定入口以 `scripts/README.md` 的 System eval 索引为准。新增原生 LangSmith/Release Review 前，必须直接消费
Agent Server 或 `NativeGraphEvaluationTarget` 的标准 messages/native trace，并重新定义 Dataset、Feedback、
副作用与 cleanup 合同；不得恢复通用 Runtime facade、旧 turn state 或旧 Tool fixture backend。
