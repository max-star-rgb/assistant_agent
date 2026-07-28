# Langfuse 案例目录

本目录只保存 Dataset item 定义，不保存 Dataset、Suite、Profile、Evaluator 或运行结果。

```text
cases/
  legacy/       # eval engineering workflow 建立前的待迁移案例
  engineered/   # 按 capability、独立证据和校准流程设计的新案例
```

规则：

- `legacy/behavior_pre_eval_engineering.cases.json` 是可整体删除的迁移边界；
- `engineered/` 默认一个 capability 一个版本化 case 文件，可包含该 capability 的必要回归变体；
- 新案例不得加入 `legacy/`，也不得直接写入 `datasets/`；
- Dataset composition 在 `../datasets/*.dataset.json` 中按顺序引用 case source；
- 将 legacy 案例完成重设计、校准和真实 Experiment 审计后，在 `engineered/` 创建对应文件，再从
  legacy collection 删除原 item；
- legacy item 全部迁移后，删除 `legacy/` 并从 behavior composition 移除该 source；
- 删除本地 legacy source 不删除 Langfuse 中已有的历史 Dataset、Experiment 或 Score。

`draft -> calibrated -> active -> retired` 是 engineered case 的生命周期，不是目录层级。Fixture 放在
`../fixtures/<capability>/`，Judge 校准样本放在 `../evaluators/calibration/`。

## Engineered case 说明范式

新案例按“场景契约 + 评判契约”说明，不用关键词或完整参考回答代替成功边界：

| concern | field | meaning |
| --- | --- | --- |
| Capability | `metadata.capability` | 一个可命名、可区分的被测行为 |
| Request | `input.user_request` | 实际发送给 Agent 的请求和可见环境 |
| Dependencies | `metadata.compatible_profiles`、`dependency_mode`、`dependency_contract`、`required_tools`、`effect_scope` | profile、live/frozen/simulated 依赖、具体夹具与是否调用真实外部服务、运行所需工具和副作用边界；`required_tools` 不表示 Agent 必须调用 |
| Success | `expected_output.evaluation_contract.pass_iff` | 由独立证据观察到的唯一通过边界 |
| Evidence | `expected_output.evaluation_contract.evidence_by_score` | 四层 Score 各自读取的证据字段和通过条件 |
| Recommendation | `metadata.lifecycle`、`calibration_fixture` | draft/calibrated/active/retired 状态与校准来源 |

`evaluation_contract` 使用
`assistant_agent_case_evaluation_contract_v1`。四个 `evidence_by_score` 条目必须齐全，避免机械层、
工具语义层和回答语义层重复判断同一事实。该契约供 Evaluator/Judge 使用，不会由 Experiment task
传入 `UserRequest`；Judge rubric、隐藏证据和校准样本也不得放进 `input.user_request`。

`input.user_request` 是唯一传给 Agent 的案例请求。`input.evaluation_criteria`、整个
`expected_output` 和 `metadata` 都是评测侧字段；其中 `expected_output` 保存隐藏真值、冻结夹具或
受控故障，`metadata.dependency_contract` 必须明确依赖模式、fixture 标识和
`live_*_called` 布尔值。这样查看本地案例或 Langfuse Dataset item 时，可以直接区分自然的真实依赖
调用、冻结本地输入、隔离本地状态和受控故障注入。
