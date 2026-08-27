# Evaluation Authority

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | System eval 与原生 Graph evaluation target 的当前权威 |
| Owns | 真实能力专项验证、operator 门禁、system artifact、原生 Graph evaluation target |
| Does not own | 默认 pytest、生产 Graph 实现、未来 unified behavior eval 的 Dataset/Experiment 设计 |
| 源码与 schema 入口 | `evals/system/`、`src/assistant_agent/evaluation/` |
| 验证入口 | `docs/authority.toml` 中 `system-eval.verification` |
| 相邻 authority | `tests/README.md`、`docs/runtime-event-stream-architecture.md`、`docs/observability-harness.md` |

## 当前验证分层

默认 pytest 与临时 TDD 由 `tests/README.md` 管理。System eval 只在 operator 明确授权下验证 Provider、Tool、
Memory 或媒体真实能力，并把有限、脱敏的结果写入 `.data/evals/system/`。`evals/system/tools/` 另保留离线 Tool
冒烟：每个当前业务 Tool 通过标准 `ToolNode` 执行固定输入，只检查成功返回，不复制 Deep Agents filesystem 等
框架 Tool 的集成覆盖。`run_all.py` 递归运行该目录的非 helper 冒烟脚本。

仓库继续提供 `NativeGraphEvaluationTarget`，它直接调用生产 `AssistantRootGraph`，不经过旧 Runtime facade 或
mock fallback。旧 Runtime Regression、Workflow Regression、Release Review 和 CodingGraph behavior baseline
runner 均已删除；当前没有上线前统一 Agent 行为门禁。

未来若建立 unified coding behavior eval，必须直接消费 `assistant-native-v4` 或
`NativeGraphEvaluationTarget` 的标准 messages、原生 trace、当前 state 与 interrupt/resume 合同，并重新定义
Dataset、Feedback、副作用审批、sandbox 与 cleanup 证据。不得复用旧 graph mode、旧 checkpoint state、旧 fixture
catalog 或已删除的 attestation route 来宣称当前行为质量。

## 真实运行安全

真实 System eval 必须同时满足：

- `MULTIMODAL_AGENT_PROVIDER_MODE=real`；
- 对应 Provider/Tool 配置完整；
- runner 的 real/staging 显式开关；
- operator 明确确认调用范围与副作用；
- 使用本机未跟踪配置，不提交 key、原始响应、真实用户数据或远端 artifact。

普通 pytest、`--help`、schema 校验和 `--dry-run` 保持 mock/offline。不得检测到 key 后自动启用真实调用，也不得
用 mock fallback 冒充真实能力验证。稳定入口以 `scripts/README.md` 的 System eval 索引为准。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python scripts/run_system_calendar_create_eval.py --dry-run
python -m compileall -q src/assistant_agent/evaluation evals/system
```
