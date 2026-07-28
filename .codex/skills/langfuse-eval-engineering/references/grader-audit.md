# Grader 设计与审计

## 分层判断

优先使用确定性检查验证可客观证明的事实：工具暴露、调用次数、参数、错误码、终态和状态变化。只有
回答含义、任务完成度或开放式质量才交给语义 Judge。

Task-local grader 汇总全部检查，只输出一个主要结果：

```text
agent_eval.reward = 1.0 或 0.0
agent_eval.check.<name> = 诊断布尔值
```

主要 reward 是唯一门槛；诊断检查解释为什么失败。

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

- Task 条件满足但 Agent 行为错误：Agent fail；
- Environment 未按声明运行：infrastructure fail；
- Judge 超时、输出不可解析或标签不稳定：infrastructure fail；
- 缺少 `agent_eval.reward`：infrastructure fail；
- 外部依赖故障只有在 Environment 明确规定恢复行为时，才参与 Agent 判定。
