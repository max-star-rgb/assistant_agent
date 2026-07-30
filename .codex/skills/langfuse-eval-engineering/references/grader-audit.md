# Grader 设计与审计

## 四个独立 Score

先验证 Environment、受控依赖、隔离和评测输入；失败属于 infrastructure failure，不生成 Agent
Score。正式评分固定输出四个 BOOLEAN Score，不生成 reward 或总通过分：

```text
agent_eval.dimension.tool_execution
agent_eval.dimension.tool_semantics
agent_eval.dimension.grounding
agent_eval.dimension.response_quality
```

- `tool_execution`：基础 Task 的实际工具终态是否符合 Environment 为该案例声明的 oracle，使用 Rule；
  Mission 还合入 Environment 的 objective state Rule；
- `tool_semantics`：工具返回内容是否语义正确、内部一致且可用，使用 LLM Judge；
- `grounding`：最终回答是否忠于工具结果，使用 LLM Judge；
- `response_quality`：回答是否真正回应当前请求并且清晰完整，使用 LLM Judge。

四项都采用阳性语义。预期天气超时且实际错误码匹配时，`tool_execution=true`；
超时没有产生可用天气数据，因此 `tool_semantics=false`；Agent 正确理解超时时，
`grounding=true`。不要把四项聚合成 pass 或 reward。

## Environment oracle

Environment 为每个可见工具声明强类型结果预期：

```python
ToolOutcomeExpectation.must_succeed("weather")

ToolOutcomeExpectation.must_fail_with(
    "weather",
    error_code="provider_timeout",
)
```

该预期不进入 Agent input 或 Dataset metadata。所有正式评分入口通过 `grade_task()`，由它比较
`tool.finished/tool.failed/error_code` 并生成唯一的 `tool_execution` Rule assertion。预期覆盖
不完整、重复或与可见工具不一致属于 infrastructure failure。

`tool_execution` 只回答“受控案例是否按 oracle 发生”，不回答工具数据是否有用。不要在 Task grader
重复判断次数、顺序、参数、状态、成功或错误码。Mission objective Rule 由 Environment 的非空
`objective_state_assertions()` 独占；Task grader 不得拥有或重复 state oracle。

## 三个 Judge

每个 Task 固定调用三个 criterion：

```text
judge.tool_semantics
judge.grounding
judge.response_quality
```

`tool_semantics` 和 `grounding` 使用通用 rubric；Task grader 只提供
`response_quality` 的专属通过条件。三个 Judge 都接收同一份结构化 Evidence，但职责不得重叠：

- 合法空结果可以通过 `tool_semantics`，Provider 超时或损坏数据不能通过；
- `grounding` 不评价回答是否完整，只检查对工具成功、失败、空结果和字段的陈述；
- `response_quality` 不因工具失败自动失败，只检查 Agent 是否在现有条件下清晰完整地回应用户。

Judge 超时、解析失败、缺少 verdict 或 criterion 不匹配属于 infrastructure failure，不能记录为
Score false。

## 可读性

每项 assertion 提供面向评测查看者的短 `label`。Score comment 通过时列出 label，失败时展示
`label + reason`；内部 assertion key 不得单独充当 comment。Score metadata 只保存
`passed/label/method/criterion_id` 标量，不传播 rubric、长 reason 或嵌套对象。

## 校准

Calibration v3 至少包含一个阳性样本和一个可信反例。每个 fixture 显式保存：

```text
expected_dimensions:
  tool_execution
  tool_semantics
  grounding
  response_quality

judge_verdicts:
  tool_semantics
  grounding
  response_quality
```

逐项比较实际结果与人工标签。不要恢复旧的总通过字段，也不要通过聚合逻辑掩盖不匹配。

## 隐藏与独立性

- Agent 输入不得包含 rubric、oracle、校准标签或预期结论；
- Judge 只接收完成判定所需的 Evidence；
- Agent 的自述不能证明工具终态；
- Trace 用于提供证据，不直接充当正确答案。
- Langfuse Dataset item 只保存 `task_id + request + 短 metadata`，不复制 case level、state oracle 或
  rubric。
