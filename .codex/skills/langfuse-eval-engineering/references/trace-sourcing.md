# Trace 取样

只有用户提供 Trace 范围或明确授权时才使用。Trace 用来发现真实请求、失败模式和候选 capability，
不建立正确答案。

若已给出 project、run/trace ID、Dataset run、时间窗口或本地文件，就限制在该范围。否则先说明
来源、时间窗口、首批最多 25 条完整 Trace、所需字段和临时导出删除时机。不要打印凭据，也不要把
原始 Trace 写入仓库。

基于真实运行失败回答“为什么”时，先按 `AGENTS.md` 检查可对应问题的最新 `.data/**` 机器日志，
Langfuse 只补充 observation 层级和 Score。

优先比较正常完成、用户纠正、工具失败或重复、Provider/网络失败的 Trace。保留必要的 trace/run ID、
代码和模型 revision；只提取用户输入、工具/Validator 轨迹、状态变化、回答和已有 Score。

分析只输出：

```text
Observed behavior: 实际请求与行为
Comparison: 成功和失败的差异
Attribution: Agent、依赖、grader 或未知
Task candidate: 可独立判断的 capability
```

从 Trace 派生 Task 时，使用脱敏的合成请求和受控 Environment 重现行为；不要复制用户数据、原始
Provider 响应或历史目标回答。
