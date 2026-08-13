# Evaluation Authority

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | System eval、LangSmith Runtime Regression 与 Release Review 的当前权威 |
| Owns | 真实能力专项验证、LangSmith Runtime Regression、Release Scenario/Dataset/Experiment、Feedback 完整性与发布决策记录 |
| Does not own | 默认 pytest 策略、Runtime 实现、trace schema、Provider/Tool 配置 |
| 源码与 schema 入口 | `evals/system/`、`evals/langsmith_runtime_regression/`、`evals/release_review/` |
| 验证入口 | `docs/authority.toml` 中 `release-review-eval.verification` |
| 相邻 authority | `tests/README.md`、`docs/observability-harness.md`、`docs/tool-calling-architecture.md` |

## 1. 三类验证

| 类型 | 问题 | 入口与事实源 |
| --- | --- | --- |
| System eval | 特定真实能力在明确环境中是否工作 | `evals/system/`；本地 artifact |
| Runtime Regression | 已人工确认的日常异常在当前生产 Runtime 是否复现或修复 | `evals/langsmith_runtime_regression/`；LangSmith Dataset、actual graph Experiment 与 Feedback |
| Release Review | 选定版本的 Agent 行为是否达到上线要求 | `evals/release_review/`；Git Scenario、LangSmith Dataset/Experiment/Feedback 与本地 decision artifact |

默认 pytest 和 core invariant 见 `tests/README.md`。`evals/system/incubating/<feature>/` 是可删除的节点专项，
不能伪装成 core 或 Release Review。

## 2. 真实运行安全

System eval 与 Release Review 只有在 operator 明确授权后才允许真实 Provider。必须同时满足：

- `MULTIMODAL_AGENT_PROVIDER_MODE=real`；
- 对应 Provider 与 Tool 配置完整；
- CLI 的 real-provider/staging 显式开关；
- 使用本机未跟踪配置，禁止提交 key、原始响应、真实用户数据或远端 artifact。

普通 pytest、`--inspect`、schema 校验与本任务验证必须保持 mock/offline。禁止通过检测 key 自动启用真实调用，
禁止 mock fallback 冒充真实验证。

## 3. 日常异常到 Runtime Regression 的唯一闭环

日常异常的唯一评测入口是：

1. operator 在 LangSmith UI 或 SDK 中人工定位 actual graph trace；
2. 人工复核失败事实、去除敏感内容并决定是否值得长期回归；
3. 将确认案例沉淀到固定 Runtime Regression Dataset；
4. 使用 `scripts/run_langsmith_runtime_regressions.py` / `evals/langsmith_runtime_regression` 在生产
   `AgentGraphRuntime` 上运行；
5. 以持久化 Experiment root run、actual graph child runs 与完整 Feedback 判断结果。

仓库不提供自动 runtime audit、定时抓取、远端 webhook 或平台间 Dataset 同步。没有人工确认的 trace 不会自动
成为测试。Runtime Regression Dataset 的 active Example 是 runner 的唯一输入；inspect 只读，不修改远端。

常用离线入口：

```bash
python scripts/run_langsmith_runtime_regressions.py --inspect
```

配置 evaluator 或执行真实回放属于远端写入/真实 Provider 行为，必须由 operator 另行授权。

## 4. Release Review

Release Review 的 Git YAML Scenario 是评审意图与安全约束的版本化权威；LangSmith 保存由这些 Scenario 同步的
Example、actual graph Experiment、run 与 Feedback。`sync_langsmith_examples()` 对仓库拥有的 Example 执行
幂等 create/update，并把不再存在的 owned Example 标记 inactive；不修改外部拥有的 Example。

runner 通过生产 `AgentGraphRuntime` 的 actual compiled graph 执行 Decision 与 Staging 场景。每个 Example
必须有一个 root run、完整 native child tree 和要求的 Feedback；缺失、重复、rate limit、Provider/Tool 配置
失败或 cleanup 失败均归类为 infrastructure failure，不能解释成质量通过。

稳定入口：

```bash
python scripts/run_release_review.py --inspect
python scripts/run_release_review.py --sync --git-commit <sha>
python scripts/run_release_review.py --preflight --release-id <id> --allow-real-provider
python scripts/run_release_review.py --run --release-id <id> --allow-real-provider
```

`--sync`、evaluator 配置和 `--run` 都会写入远端；Staging side effect 还需
`--allow-staging-side-effects`。这些命令不属于默认验证。

Release Review 继续保留 actual graph 评审，但不要求 Git Workflow oracle，不存在 Workflow P3 作为 Task 11
或后续发布的 hard gate。历史实验结论不进入当前 authority；需要回顾时只读对应历史材料。

## 5. Feedback 与结果

Runtime Regression 与 Release Review 均使用 `assistant_agent.quality.*` Feedback key。质量失败和基础设施失败
必须分开：只有运行树与 required Feedback 完整时，质量分数才可用于结论。Release decision 由人工记录在
`.data/evals/release_review/<release_id>/decision.json`，内容仅包含安全 ID、决定、operator、时间与有限 note。

报告不得保存 secret、Provider 原始 payload 或未脱敏用户内容。真实验证最终汇报必须写明 Provider、场景、
副作用、cleanup 与 artifact 路径。

## 6. 维护与验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/tdd/langsmith-parallel-evaluation
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/tdd/release-review-native-experiment
python scripts/run_release_review.py --inspect
python scripts/run_langsmith_runtime_regressions.py --inspect
```

Dataset schema、Example ownership、actual graph target、Feedback key、completeness 或 decision contract 变化时，
同步本文件。不要修改历史 `docs/superpowers/**` 或 `.superpowers/**` 来伪造当前结论。
