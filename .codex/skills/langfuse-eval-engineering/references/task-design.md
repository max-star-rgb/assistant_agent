# Task 与 Environment 设计

## Task 是什么

Task 是一个用户可理解、结果可判断的能力挑战。它只包含真实用户请求、稳定 ID、capability 和运行
入口，不包含正确答案、依赖故障说明、grader rubric 或内部 profile。

好的 Task 必须满足：

- 没有所选能力就很难稳定完成；
- 成功与失败能由 Agent 外部证据区分；
- 一次只改变一个主要能力变量；
- 请求像真实用户，而不是测试指令。

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
检查受控依赖、Tool Registry、隔离和复位前提；验证失败时不得生成 Agent Score。

## Evidence

统一 Evidence 只投影稳定事实：

- runtime 终态；
- 可见工具；
- 工具调用名、参数、顺序、Validator 结果和终态；
- 依赖错误码或结构化结果；
- 初始/最终状态及 diff；
- 最终回答；
- 必要的 Provider 结果类型。

不要把整条原始 Trace 或 Environment 私有配置复制进 Dataset metadata。
