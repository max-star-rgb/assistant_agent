# Tool 重复调用策略设计

## 目标

把 Runtime 中按工具名硬编码的单次调用限制收敛为 Tool 自己声明的策略：默认每个 Tool 在一个 run 中
最多成功执行一次；明确声明为可重复的 Tool 可以用不同参数继续调用，直到耗尽全局
`max_tool_iterations`。参数完全相同且已有完整成功结果的调用继续复用已有 observation，不再次执行。

## 契约

`ToolSpec` 新增 `repeat_policy`：

- `once_per_run`：默认值。该 Tool 在当前 run 已成功执行后，后续任何参数的调用都被阻止并进入
  finalize；失败调用仍由现有 recoverable/non-recoverable 与相同失败签名规则处理。
- `distinct_inputs`：允许同一 Tool 在当前 run 中使用不同的规范化输入多次执行；参数完全相同且已有
  完整成功结果时仍被现有 duplicate guard 阻止。

该字段由 Tool/Plugin/MCP adapter 在 ToolSpec 构造边界声明，不使用按工具名维护的中心表。它属于
Runtime 治理契约，不进入 Provider Tool schema，也不要求 LLM 理解策略。

`shopping_search` 声明 `distinct_inputs`。其他本地 Tool 使用默认 `once_per_run`。全局
`max_tool_iterations` 始终具有更高优先级，因此“可重复”不代表无限预算。

## Runtime 行为

Runtime 在已有成功 ToolResult 后记录本 run 已成功执行的 Tool 名。下一次决策进入 guard 时，从当前
run 的 Registry 读取 ToolSpec：

1. 先应用全局 tool-call budget；
2. `once_per_run` 且该工具已成功执行时，以稳定原因码 `tool_repeat_limit_reached` 拒绝；
3. `distinct_inputs` 继续执行现有相同成功输入去重；
4. non-recoverable failure 和相同失败输入仍按现有规则阻止。

删除 `shopping_search` 的专用分支和专用提示文字。Guard 只依据 ToolSpec，不枚举内置、MCP 或 Plugin
工具名。

## 可观测性与兼容性

拒绝产生结构化 observation，错误码统一为 `tool_repeat_limit_reached`，并沿用现有 loop-guard trace。
旧的 `run_tool_call_limit_reached` 不再由购物专用路径产生。ToolSpec 缺少新字段的旧 dict/MCP 投影通过
Pydantic 默认值兼容为 `once_per_run`。

## 测试

在 `tests/tdd/tool-repeat-policy/` 做临时 RED/GREEN：

- 默认 Tool 第二次即使参数不同也被拒绝；
- `distinct_inputs` Tool 使用不同参数可以重复执行；
- `distinct_inputs` Tool 使用相同成功参数仍被去重；
- `shopping_search` 的 ToolSpec 声明为 `distinct_inputs`；
- 无论策略如何，都不能超过 `max_tool_iterations`。

本次不改变已登记 core invariant，不修改 `tests/core`。
