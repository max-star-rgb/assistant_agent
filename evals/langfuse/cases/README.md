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

engineered source 必须使用 `assistant_agent_eval_case_collection_v2`，并通过
`contracts.py` 的强 schema 校验。新案例按“场景契约 + 评判契约”说明，不用关键词或完整参考回答
代替成功边界：

| concern | field | meaning |
| --- | --- | --- |
| Capability | `metadata.capability` | 一个可命名、可区分的被测行为 |
| Summary | `metadata.scenario_summary` | 在 Langfuse 列表中可直接理解的中文场景说明 |
| Request | `input.user_request` | 实际发送给 Agent 的请求和可见环境 |
| Dependencies | `metadata.dependency_summary`、`dependency_types`、`fixture_ids` | 以短中文摘要和稳定枚举说明真实 Chat、Tool 服务及受控 fixture |
| Live calls | `metadata.uses_live_chat_provider`、`uses_live_external_tool_service` | 分别明确是否调用真实 Chat Provider 和真实外部 Tool 服务 |
| Execution | `metadata.compatible_profiles`、`required_tools`、`forbidden_tools`、`effect_scope` | profile、工具候选和副作用边界；`required_tools` 不表示 Agent 必须调用 |
| Success | `expected_output.evaluation_contract.pass_iff` | 由独立证据观察到的唯一通过边界 |
| Evidence | `expected_output.evaluation_contract.evidence_by_score` | 四层 Score 各自读取的证据字段和通过条件 |
| Oracle | `expected_output.oracle` | 统一保存隐藏 fixture、ground truth、必需/禁止事实和状态约束 |
| Recommendation | `metadata.lifecycle`、`calibration_fixture` | draft/calibrated/active/retired 状态与校准来源 |

engineered item 的 `input` 只允许 `user_request`，不得包含 `evaluation_criteria`、Judge rubric 或
隐藏证据。`expected_output` 固定只允许三个顶层字段：
`schema_version=assistant_agent_case_expectation_v2`、`evaluation_contract` 和 `oracle`。
`metadata` 使用 `assistant_agent_case_metadata_v2`，不再使用含义模糊且重复的
`dependency_mode`、`dependency_contract` 或 `fixture_version`。

`evaluation_contract` 继续使用
`assistant_agent_case_evaluation_contract_v1`。四个 `evidence_by_score` 条目必须齐全，避免机械层、
工具语义层和回答语义层重复判断同一事实。该契约供 Evaluator/Judge 使用，不会由 Experiment task
传入 `UserRequest`；Judge rubric、隐藏证据和校准样本也不得放进 `input.user_request`。

`dependency_types` 当前允许 `live_chat_provider`、`frozen_file_fixture`、
`isolated_local_state`、`injected_tool_failure` 和 `live_tool_service`。真实 behavior case 必须通过
两个 `uses_live_*` 布尔字段分别说明 Chat 和 Tool 的真实调用边界，不能因为 Tool 数据使用 fixture
就把整个案例描述成离线。

Langfuse 会把 Dataset item metadata 传播成 Trace attribute，单个值超过 200 字符时会丢弃。因此
metadata 禁止嵌套详细依赖对象，`scenario_summary` 和 `dependency_summary` 最长 180 字符，其他
字段只使用短标量或短列表；路径、SHA、故障载荷和状态约束放在 `expected_output.oracle`。v2 schema
会逐字段验证传播后的长度。legacy collection 暂时保留 v1 自由格式，迁移到 engineered 时必须
一次性转换成上述 v2 结构。
