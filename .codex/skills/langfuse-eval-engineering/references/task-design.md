# Task 与 Environment 设计

## Task 是什么

Task 是一个用户可理解、结果可判断的能力挑战。它只包含真实用户请求、稳定 ID、capability 和运行
入口，不包含正确答案、依赖故障说明、grader rubric 或内部 profile。

好的 Task 必须满足：

- 没有所选能力就很难稳定完成；
- 成功与失败能由 Agent 外部证据区分；
- 一次只改变一个主要能力变量；
- 请求像真实用户，而不是测试指令。

## 选择 Task 或 Mission

`tasks/` 与 `missions/` 共用 loader、Environment、Evidence、校准、发布和运行协议，ID 跨目录唯一。
只有当成功还要求由结构化 state Evidence 证明客观终态时才选择 Mission；否则使用基础 Task。Mission
Environment 必须拥有非空、只含 Rule assertion 的 `objective_state_assertions()`，并以其声明目标状态
oracle；grader 不拥有该 oracle。

## Environment 是什么

Environment 定义 Agent 在什么世界中运行：

- 使用哪个活动 runtime 和公开入口；
- 暴露哪些工具；
- 依赖是 live、frozen 还是 simulated；
- 初始状态、可写范围和复位方式；
- 哪些故障是固定注入的；
- 哪些证据在运行后可读取。

Environment 可以模拟依赖，但不能模拟被测 Agent 决策。写操作必须在每个 Task run 使用可丢弃或可
恢复状态。只读动态数据应记录来源和时间。每个 Environment 应提供不运行 Agent 的 `validate()`，
检查受控依赖、Tool Registry、隔离和复位前提；验证失败时不得生成 Agent Score。每个可见工具还
必须通过 `tool_outcome_expectations()` 声明 `must_succeed` 或带明确错误码的
`must_fail_with`；该声明是工具业务结果预期的唯一事实源。

基础 Task 默认继承 `ControlledTaskEnvironment`。共享模板唯一拥有
`describe/validate/tool_outcome_expectations/execute`，Task-local Environment 只通过 hook 声明受控
依赖、registry replacement、必需成功/失败、Task 专属 Rule、初始/最终状态与 runtime override。
不要在每个任务中复制公共生命周期；需要改变工具暴露时实现结构化 `visibility_override()`。

Mission Environment 还必须把结构化 `initial_state`、`final_state` 或 `state_diff` 转成非空、Rule-only 的
`objective_state_assertions()`。基础 Task 不要求该方法；Mission 缺少该方法、返回空结果或混入 Judge
assertion 都是 infrastructure failure。

所有 Task 的默认 Environment 必须从共享完整 Agent eval Tool Registry 装配工具，不得根据 Task
capability、目标工具、rubric 或用户请求文本裁剪目录。目标工具由 Task 接入确定性依赖，其余工具也
必须保持受控且具备非必调 outcome expectation。媒体、entry profile、durable ready-step 等结构化
运行条件可以在具体 run 中收窄实际可见集合；评分时可以据 Evidence 的 `available_tools` 生成对应
子集，但无参数的默认 `tool_outcome_expectations()` 必须覆盖完整默认目录。

需要预留精细化控制时，复用 runtime 的结构化
`metadata.tool_visibility.profile + allowed_tools`，不要另建基于用户文本或 capability 的路由。
override 由 Environment 或受信入口拥有：profile 必须可读，allowlist 必须是已注册且依赖受控工具的
子集，`validate()` 必须证明配置有效，运行后的 Evidence 必须记录实际可见集合，Environment 必须为
该集合提供完整 outcome expectation。它只能收窄当前已装配目录，不能注册新工具、绕过 Provider/
权限开关或扩大真实副作用；控制配置不得进入 `task.json` 请求、Dataset metadata 或 grader rubric。

## Evidence

统一 Evidence 只投影稳定事实：

- runtime 终态；
- 可见工具；
- 工具调用名、参数、顺序、Validator 结果和终态；
- 依赖错误码或结构化结果；
- 初始/最终状态及 diff；
- 最终回答；
- 必要的 Provider 结果类型。

不要把整条原始 Trace 或 Environment 私有配置复制进 Dataset metadata。Langfuse Dataset item 只保存
`task_id + request + 短 metadata`；不要复制 case level、state oracle 或 grader rubric。

Evidence 进入评分层后，确定性 Git Rule 和原生 Langfuse Evaluator 必须分开：Trace、参数、错误码和
状态等客观事实由 Rule 判断；只有结果解释、证据使用和回答质量等开放语义交给 Evaluator。两者统一
使用按 Agent 行为命名的固定维度，不按判断手段创建 Score。
