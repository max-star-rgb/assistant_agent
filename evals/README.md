# Evaluation Authority

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | System eval 与原生 Graph 评测 target 的当前权威 |
| Owns | 真实能力专项验证、operator 门禁、system artifact、原生 Graph evaluation target |
| Does not own | 默认 pytest、生产 Graph 实现、LangSmith Dataset/Experiment 的未来重建方案 |
| 源码与 schema 入口 | `evals/system/`、`src/assistant_agent/evaluation/native_graph_target.py` |
| 验证入口 | `docs/authority.toml` 中 `system-eval.verification` |
| 相邻 authority | `tests/README.md`、`docs/runtime-event-stream-architecture.md`、`docs/observability-harness.md` |

## 当前验证分层

当前只保留 System eval：在 operator 明确授权下验证真实 Provider、Tool、Memory 或媒体能力，并把有限、
脱敏的结果写入 `.data/evals/system/`。默认 pytest 与临时 TDD 的边界仍由 `tests/README.md` 管理。

旧 Runtime Regression、Workflow Regression 与 Release Review runner 已随通用旧 Graph Runtime 删除。它们的
evidence、fixture backend、catalog 与终态合同绑定旧 `AgentState`，不能通过兼容 facade 投影到新图。仓库提供
`NativeGraphEvaluationTarget` 作为后续重建基元：它直接调用生产 `AssistantRootGraph`，只返回标准 messages 和
thread/run identity，不拥有产品状态机、trace store 或 checkpoint facade。当前没有上线前行为评审门禁，发布方
不得把已删除 runner 的历史结论描述为当前批准。

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
