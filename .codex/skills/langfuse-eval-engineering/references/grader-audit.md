# Grader 设计与审计

## 三层判断

先验证 Environment、受控依赖、隔离和评测输入是否有效；失败属于 infrastructure failure，不生成
Agent Score。随后使用确定性 assertion 验证可客观证明的工具轨迹和状态；只有回答含义、任务完成度
或开放式质量才交给语义 Judge。

Task-local grader 把专属 assertion 聚合到固定四维。Langfuse 只输出：

```text
agent_eval.reward = 1.0 或 0.0
agent_eval.dimension.tool_execution
agent_eval.dimension.tool_use
agent_eval.dimension.state
agent_eval.dimension.response
```

- `tool_execution`：工具暴露、Validator、调用和结构化终态是否完整；业务依赖允许按 Task 预期失败；
- `tool_use`：Agent 的工具选择、参数、次数、顺序、结果消费和恢复策略是否符合任务；
- `state`：最终状态转换是否符合任务，包括预期不变；
- `response`：最终回答是否忠于证据并完成用户目标。

主要 reward 是四个必要维度的确定性聚合，也是唯一门槛。天气参数、日历写入内容等 Task 专属 assertion
只进入维度详情和 comment，不创建新的 Langfuse Score。

不要把“工具业务结果成功”误写成 `tool_execution`。例如天气依赖按 Environment 固定超时时，只要
Validator、执行和 `tool.failed/provider_timeout` 结构化闭合，`tool_execution` 可以通过；是否正确
停止重试、消费失败结果和恢复由 `tool_use`、`response` 判断。

## Rule 与 LLM Judge

Rule 与 LLM Judge 是判断机制，不是顶层质量维度。两者分开实现并统一产出 assertion：

- 可从 Trace、结构化 Tool result 或状态直接证明的事实使用 Rule；
- 结果解释、证据忠实性、任务完成度等开放语义使用 LLM Judge；
- 每条 assertion 标记 `evaluation_method=rule|judge`；
- 每条 assertion 提供简短、面向评测查看者的 `label`；内部 assertion key 只用于稳定定位，不得
  单独充当 Langfuse comment；
- Judge assertion 使用稳定 `criterion_id`，校准文件为每个 criterion 单独提供
  `judge_verdicts`；
- Rule 失败不能被 Judge 覆盖；Judge 超时、解析失败、缺少 verdict 或 criterion 不匹配属于
  infrastructure failure。

同一行为维度可以同时包含 Rule 和 Judge。例如 `tool_use` 可以包含参数 Rule、调用策略 Rule 和
`outcome_evidence_usage` Judge。不要因为判断机制不同而创建 `rule_score` 或 `llm_score`。

维度失败 comment 必须展示失败数量、`label` 和 assertion 的真实 `reason`；主要 reward comment
必须展示失败维度的中文名及其失败 assertion 摘要。不要只输出 `response_quality`、
`outcome_evidence_usage` 等内部 ID。

## 工具结果单一事实源

Environment 必须为每个可见工具声明一个强类型结果预期：

```python
ToolOutcomeExpectation.must_succeed("weather")

ToolOutcomeExpectation.must_fail_with(
    "weather",
    error_code="provider_timeout",
)
```

该预期不进入 Agent input 或 Dataset metadata。所有正式评分入口必须通过通用 `grade_task()`，由它
确定性比较 `tool.finished/tool.failed/error_code`，并自动增加
`tool_use.outcome_matches_environment`。预期成功但实际超时必须使 `tool_use` 失败；
预期超时且错误码匹配只说明业务结果符合场景，Agent 的调用次数、参数、恢复和回答仍由其他 assertion
判断。

`outcome_matches_environment` 不证明 Agent 理解了工具结果。Task 若要求结果消费或 grounding，应
增加独立的 `outcome_evidence_usage` Judge assertion；它只判断 Agent 是否把可用工具 Evidence
正确用于后续行为，不代替最终回答质量判断。

Environment outcome 与可见工具覆盖不完整、重复或不一致属于评测配置错误，必须 fail closed。Task
grader 只描述调用策略、状态和回答，不重复声明成功、失败或错误码。

## 隐藏与独立性

- Agent 输入不得包含 rubric、oracle、校准标签或预期结论；
- Judge 只接收完成判定所需的最小 Evidence；
- Agent 的自述不能证明它调用了工具或改变了状态；
- 历史目标回答不能直接作为正确答案。

## 直接校准

在运行真实 Experiment 前，把 grader 直接应用于人工标注 Evidence：

- 至少一个明确正确样本；
- 至少一个表面合理但违反关键约束的样本；
- 涉及轨迹时，再加入“回答正确但调用/状态错误”的样本。

同时比较最终 pass 标签和所有命名 Judge criterion 标签。任何校准不匹配都先修 grader、rubric 或
Evidence，不要通过放宽主要 reward 掩盖问题。

## 失败归属

- Task 条件满足但任一必要维度失败：Agent fail；
- Environment 未按声明运行：infrastructure fail；
- Judge 超时、输出不可解析或标签不稳定：infrastructure fail；
- 缺少 `agent_eval.reward`：infrastructure fail；
- 外部依赖故障只有在 Environment 明确规定恢复行为时，才参与 Agent 判定。
