# 评分设计与审计

## 两层评分权威

评测输入、受控依赖、隔离或 Evidence 构造失败属于 infrastructure failure，不生成 Agent 失败分。
正式 Experiment 固定要求三个相互独立的 BOOLEAN task-level Score：

```text
assistant_agent.quality.task_conformance
assistant_agent.quality.grounding
assistant_agent.quality.response_quality
```

- `task_conformance`：仓库中的确定性 Rule 比较工具 outcome、Environment oracle 与 Mission objective；
- `grounding`：Langfuse 原生 Evaluator 判断回答是否忠于工具结果、上下文和结构化终态；
- `response_quality`：Langfuse 原生 Evaluator 判断回答是否清晰、完整地回应当前请求。

各项保持阳性语义，不聚合为 pass、reward 或总分。单个工具 observation 的语义质量由
`assistant_agent.quality.tool_result_quality` Live Evaluator 独立负责。

## Environment oracle

Environment 为每个可见工具声明强类型结果预期：

```python
ToolOutcomeExpectation.must_succeed("weather")

ToolOutcomeExpectation.must_fail_with(
    "weather",
    error_code="provider_timeout",
)
```

该预期不进入 Agent input 或 Dataset metadata。`grade_task_conformance()` 比较
`tool.finished/tool.failed/error_code`，并为 Mission 合入非空的 `objective_state_assertions()`。
预期覆盖不完整、重复或与可见工具不一致属于 infrastructure failure。Task 不得另建 grader 重复
次数、顺序、参数、状态、成功、错误码或 objective state oracle。

## 原生 Evaluator

`grounding` 与 `response_quality` 各自只有一个版本化 Evaluator family。Live Rule 与 Experiment Rule
引用相同 family，使日常 Trace 和正式 Experiment 使用同一 prompt、模型与输出定义。仓库管理 Rule
的 evaluator reference、target、filter 与 mapping；Langfuse UI 管理已有 Rule 的 `enabled/sampling`。

Evaluator 超时、解析失败、Rule 暂停或 Score 未在等待窗口内产生都属于 infrastructure failure，不能
记录为 `false`。Experiment 完成后用 Observations v2 定位 `experiment-item-task`，再用 Scores v3
同时按 `trace_id + observation_id` 回查三项 Score。

## 校准

Calibration v3 至少包含一个阳性样本和一个可信反例。旧标签投影为：

```text
tool_execution -> task_conformance
grounding -> grounding
response_quality -> response_quality
```

校准器把 Evidence 放入 `assistant-agent-evaluator-calibration` Dataset Experiment，等待原生 Evaluator
Score，并逐项与人工标签比较。旧 `tool_semantics` 标签和本地 Provider Judge 仅属迁移兼容，不是新
Task 的评分入口。

## 隐藏、独立与 Evidence

- Agent 输入不得包含 Rule、Evaluator prompt、oracle、校准标签或预期结论；
- Agent 的自述不能证明工具终态；
- Trace 只提供证据，不直接充当正确答案；
- Evidence 只投影 runtime 终态、可见工具、调用参数与结果、依赖错误码、状态 diff 和最终回答；
- Dataset item 只保存 `task_id + request + 短 metadata`，不复制 Environment 私有配置或长 oracle。

## 迁移兼容代码删除条件

`evals/agent/judge.py`、`batch_grading.py`、旧 `grade_task()` / `run_calibration()` 以及现有 Task 的
`grader.py` 暂时保留用于读取旧定义；正式 CLI、Experiment 和新 Task 不得引用它们。待历史
`task.json.grader` 与 Calibration v3 的 `tool_semantics/judge_verdicts` 字段完成数据迁移后，一次性删除，
不长期维护两套评分机制。
