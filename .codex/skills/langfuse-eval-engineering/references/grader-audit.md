# Grader 设计与审计

## 三层判断

先验证 Environment、受控依赖、隔离和评测输入是否有效；失败属于 infrastructure failure，不生成
Agent Score。随后使用确定性 assertion 验证可客观证明的工具轨迹和状态；只有回答含义、任务完成度
或开放式质量才交给语义 Judge。

Task-local grader 把专属 assertion 聚合到固定四维。Langfuse 只输出：

```text
agent_eval.reward = 1.0 或 0.0
agent_eval.dimension.tool_execution
agent_eval.dimension.tool_semantics
agent_eval.dimension.state
agent_eval.dimension.response
```

- `tool_execution`：工具暴露、Validator、调用和结构化终态是否完整；业务依赖允许按 Task 预期失败；
- `tool_semantics`：工具选择、参数、次数、顺序和结果处理是否符合任务；
- `state`：最终状态转换是否符合任务，包括预期不变；
- `response`：最终回答是否忠于证据并完成用户目标。

主要 reward 是四个必要维度的确定性聚合，也是唯一门槛。天气参数、日历写入内容等 Task 专属 assertion
只进入维度详情和 comment，不创建新的 Langfuse Score。

不要把“工具业务结果成功”误写成 `tool_execution`。例如天气依赖按 Environment 固定超时时，只要
Validator、执行和 `tool.failed/provider_timeout` 结构化闭合，`tool_execution` 可以通过；是否正确
停止重试和恢复由 `tool_semantics`、`response` 判断。
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

同时比较最终 pass 标签和语义 Judge 标签。任何校准不匹配都先修 grader、rubric 或 Evidence，不要
通过放宽主要 reward 掩盖问题。

## 失败归属

- Task 条件满足但任一必要维度失败：Agent fail；
- Environment 未按声明运行：infrastructure fail；
- Judge 超时、输出不可解析或标签不稳定：infrastructure fail；
- 缺少 `agent_eval.reward`：infrastructure fail；
- 外部依赖故障只有在 Environment 明确规定恢复行为时，才参与 Agent 判定。
