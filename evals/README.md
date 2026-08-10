# Eval 与 Release Review

Last updated: 2026-08-10

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 正式 system eval 与上线前 Release Review 的当前权威 |
| Owns | 真实能力专项验证、Release Scenario、Langfuse Dataset/Experiment、Score 完整性、发布决策记录 |
| Does not own | pytest 分层、日常 trace 评分与 runtime audit、线上长尾诊断 |
| 源码与 schema 入口 | `evals/system/`、`evals/release_review/`、`src/assistant_agent/evaluation/` |
| 验证入口 | `docs/authority.toml` 中 `release-review-eval.verification` |
| 相邻 authority | pytest 分层见 `tests/README.md`；日常观测见 `docs/observability-harness.md` |

源码和测试高于本文；本文高于历史设计记录。评测运行不得复制 Agent loop，也不得绕过
`AgentGraphRuntime`、Provider 配置和 Tool 治理链路。

## 三层边界

| 层 | 回答的问题 | 默认安全边界 |
| --- | --- | --- |
| pytest | 确定性代码契约是否成立 | `mock/local/offline`，见 `tests/README.md` |
| system eval | 一个真实 Provider、Tool、Context、Memory 或本地模型节点是否可用 | 每项独立显式授权，产物写入 `.data/evals/system/` |
| Release Review | 待发布 Agent 是否在关键任务中选对、调用并正确使用工具 | 真实主模型；Decision 使用确定性执行后端，Staging 使用隔离资源 |

Release Review 只在上线前由 operator 显式触发，目标是在 10 分钟内形成可审核证据和风险摘要；它是
advisory gate，不自动发布或阻断部署。上线后的日常 trace、Live Observation Rule 和长尾发现属于
`docs/observability-harness.md` 所定义的另一条链路，不由 Release Review 接管。

## System eval

`evals/system/` 验证一个具体真实节点，不承担端到端 Agent 质量评分。真实 LLM 或真实外部 Tool 必须同时
满足 `MULTIMODAL_AGENT_PROVIDER_MODE=real`、本机未跟踪配置和对应脚本的 operator allow 开关；本地
SQLite 等受控能力可以按脚本契约独立运行。禁止以 mock fallback 冒充真实验证。具体脚本入口见
`scripts/README.md`，可删除的节点专项检查放在 `evals/system/incubating/<feature>/`。

## Release Review 模型

Git 是案例事实源。所有案例位于 `evals/release_review/scenarios/*.yaml`，严格 schema 位于
`evals/release_review/contracts.py`；未知字段、非法断言、缺失 fixture 或不合规 repetitions 会在运行前
失败。固定 Langfuse Dataset 名为 `assistant-agent-release-review`。`--sync` 将 Git 案例展开成 Dataset
item；同一 Git-owned 旧 item 会被归档，Langfuse UI 不能新增案例、扩大权限或覆盖 Git oracle。

每次运行只创建一个 Langfuse 原生 Dataset Run / Experiment：

- **Decision**：运行真实模型和生产工具目录，但通过注入的 `ToolExecutionBackend` 返回 YAML 中的确定性
  fixture。它检验 Agent 是否选对/漏用工具、参数、顺序、失败后的行为和回答 grounding；不注册模拟
  高德或其他同名 Tool，因此不会再发生 `Tool already registered`。默认案例只能要求当前生产目录中
  已注册的 Tool；Qwen Provider-native 联网不写成虚构的本地 `web_search` Tool，未配置的可选天气
  Tool 也不进入默认发布验收。
- **Staging**：运行真实模型及真实工具实现。写操作使用 run-scoped workflow/SQLite 资源并在结束后清理；
  高德案例只允许只读调用。Staging 禁止 fixture 和静默 mock fallback。

`risk=critical` 的 Decision 案例必须 `repetitions: 2`；其他案例为 1 或 2。当前默认并发为 4，Staging
并发上限为 2；preflight 预算 30 秒，全局预算 570 秒。超时、Langfuse 不可达、凭据、资源、Trace 或
Score 缺失都是 infrastructure failure，不能写成 Agent 质量失败。

## Score 与发布判断

每个 Dataset item 必须落库三个相互独立的 BOOLEAN task-level Score：

- `assistant_agent.quality.task_conformance`：本地确定性规则写入，检查工具、参数、顺序和状态；
- `assistant_agent.quality.grounding`：Langfuse 原生 Experiment Evaluator 判断回答是否忠于证据；
- `assistant_agent.quality.response_quality`：Langfuse 原生 Experiment Evaluator 判断回答质量。

Staging 的单工具 observation 可以额外拥有
`assistant_agent.quality.tool_result_quality`，但它不替代上述三项。runner 会通过 Langfuse observation 与
score API 回查完整性；SDK 内存结果不算落库证据。报告分别列出 critical/high、重复运行不一致和
infrastructure 风险，不计算总 reward，也不自动做发布决定。

operator 可把人工决定写入本地审计产物：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_release_review.py \
  --record-decision --release-id <release-id> --experiment-run-id <run-id> \
  --decision approved --operator <operator>
```

## 本地运行顺序

只读检查案例，不读取 `.env`、不联网：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_release_review.py --inspect
```

配置本机 Langfuse 后同步 Dataset：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_release_review.py --sync
```

正式运行必须显式确认真实 Provider 和 Staging 副作用：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_release_review.py \
  --run --release-id <release-id> \
  --allow-real-provider --allow-staging-side-effects
```

可重复使用 `--scenario <id>` 选择子集。运行前必须确认真实主模型配置完整、Langfuse 可用、所需生产工具
已注册以及 Staging 资源可清理。runner 从服务端 `ProviderConfig` 自动取得并记录实际主模型；UI 和 CLI
不得重复指定模型。结果写入 `.data/evals/release_review/<release-id>/report.{json,md}`；
远程触发 receipt/log 位于其 `remote/` 子目录。产物、真实响应、凭据和用户数据均不得提交。

## Langfuse UI 触发

内部入口固定为 `POST /internal/evals/langfuse/release-review`。服务端只有在以下本机环境变量同时满足时
接受触发：

```text
ASSISTANT_AGENT_LANGFUSE_RELEASE_REVIEW_ENABLED=true
ASSISTANT_AGENT_LANGFUSE_RELEASE_REVIEW_SIGNING_SECRET=<local-secret>
ASSISTANT_AGENT_LANGFUSE_RELEASE_REVIEW_STAGING_READY=true
MULTIMODAL_AGENT_PROVIDER_MODE=real
```

请求必须带五分钟内有效的 HMAC-SHA256 签名。Langfuse envelope 的 `datasetName` 必须是固定 Dataset，
`payload` 是 JSON 字符串，只要求 `releaseId`，可选 `scenarios` 和 `runName`。模型属于服务端配置；当前
prompt 由代码和运行上下文动态编译，不接受人工版本标签。UI Config 运行全部场景的最小值是
`{"releaseId":"<release-id>"}`。
服务端先用固定 argv 同步执行同一 CLI 的 `--preflight`，校验真实 Provider 配置、所选 Scenario 和生产
Tool catalog；失败会以非 2xx 直接返回 Langfuse，不会先接受再静默退出。preflight 通过后才返回 `202`
并异步启动 `--run`；Experiment 页面要到原生 Dataset Run 创建后才出现记录，异步 receipt/log 仍位于
`.data/evals/release_review/remote/`。签名与 body 生成稳定 trigger id，重复投递不会启动第二次。
UI 只负责选择 Dataset、Experiment Evaluator 和触发运行，不拥有 Provider 模式、Staging readiness、案例
定义或发布权限。

## 修改与验证

新增或修改案例时先运行 `--inspect`，再按 `tests/README.md` 运行
`tests/tdd/release-review-native-experiment/`。变更 runner、同步、评分或 webhook 时，还要验证 Score 审计、
幂等和固定命令边界。pytest 始终使用 mock/local/offline；只有 operator 明确执行上述正式命令时才允许
真实调用。
