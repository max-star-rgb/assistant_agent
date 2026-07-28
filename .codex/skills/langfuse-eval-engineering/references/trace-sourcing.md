# Langfuse Trace Sourcing

只有用户提供 Trace 来源或明确要求使用 Trace 时才读取并执行本参考。

Trace 用来发现真实请求、工具行为、失败模式和候选 capability，不建立正确答案。

## 确认范围

若用户已经指定 Langfuse project、Dataset run、trace/run ID、时间窗口或本地文件，就在该范围内读取。
否则先说明：

- 数据来源与时间窗口；
- 首批最多读取 25 条完整 Trace；
- 需要的字段；
- 临时导出位置和删除时机。

只询问缺失的访问或范围。不要打印凭据，不要把原始 Trace 写入仓库、Dataset seed 或 case fixture。

基于真实运行失败回答“为什么”时，先执行 `AGENTS.md` 规定的 `.data/**` 最新机器日志检查，再用
Langfuse 补充跨 observation 的层级和 Score 信息。

## 选择 Trace

优先比较：

- 正常完成的请求；
- 有用户正向反馈的请求；
- 用户纠正、改写或补充约束的请求；
- 工具返回空、失败或重复调用的请求；
- Provider、网络、限流或外部服务失败的请求。

使用现有 Langfuse API、SDK、仓库脚本或本地文件。保留 trace ID、run ID、Dataset item ID、
Experiment 名称和 Agent/model revision。完整 Trace 应尽量包含：

- 用户输入和多轮上下文；
- Agent/model 消息；
- 工具名、参数、结果、顺序、重试和错误；
- Validator/policy 结果；
- 初始/最终状态；
- 最终回答、终态、延迟和 Score；
- 用户反馈。

只有具体问题尚未回答时才取下一批；不要从小样本推断生产频率。

## 分析格式

只总结会改变案例选择或环境设计的信息：

```text
Observed behavior: 用户请求与 Agent 实际行为
Comparison: 成功与失败的差异
Attribution: Agent、依赖、Evaluator 或尚不明确
Eval candidate: 可独立判断的 capability
```

区分 Agent 失败、外部依赖失败和 Evaluator 失败。一次 429、超时或缺失 Score 不自动构成 Agent
失败；只有 Agent 存在明确的预期恢复行为时，才把外部故障转成能力案例。

使用测试、固定来源、政策、已知状态、独立计算或专家审核建立 Judge 证据。绝不把历史目标回答当作
隐藏真值。

完成分析和案例验证后，按事先说明的保留策略删除临时原始导出；仓库中只保留最小、脱敏且具有明确
来源的合成或固定评测事实。
