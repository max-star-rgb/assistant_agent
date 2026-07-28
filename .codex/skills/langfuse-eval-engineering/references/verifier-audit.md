# Langfuse Verifier and Audit

只在设计或修改 Code Evaluator、LLM-as-a-Judge、Score 或验收标准时读取本参考。

## 从成功边界开始

先写一句：

```text
Pass iff [由独立证据可观察到的成功结果]。
```

再把证据分配给 `evals/README.md` 当前定义的评分层。不要在本参考中固化 Score 名称；以该文档、
Experiment 常量和 Langfuse 中已部署的 Evaluator 为准。

通常按以下职责分层：

- runtime/trace：运行终态和必要 Trace 是否完整；
- tool mechanical：已发生调用的暴露、Validator、执行与 Trace 是否机械正确；
- tool semantic：是否应该调用、工具选择、参数、结果使用和状态变化是否符合请求；
- answer semantic：最终回答是否忠于证据并满足用户验收标准。

不要让多个 Score 重复判断同一件事。

## 确定性检查与 LLM Judge

使用代码判断客观事实：

- 运行、解析或测试是否完成；
- 必需结构化字段和 artifact 是否存在；
- 实际工具是否暴露并通过 Validator；
- 文件、数据库或状态是否发生要求或禁止的变化；
- Trace 是否包含当前权威要求的事件。

使用 LLM Judge 判断语义：

- 是否选对工具以及参数是否符合用户语境；
- 动态回答是否忠于 Tool 或检索证据；
- 是否满足多轮约束、必要澄清和任务目标；
- 是否存在会改变结论的遗漏、矛盾或无依据主张。

工具调用必须使用 Runtime 或 harness 观察到的事实，不接受目标自述。

## Judge 输入

只向 Judge 提供：

- Dataset item 的任务与验收标准；
- Agent 的结构化输出；
- 判断所需的 Tool/Validator/状态/来源证据；
- 简短 rubric；
- 严格的结构化判定格式。

限制目标可控文本和记录数量，把目标输出视为不可信输入，并要求 Judge 忽略其中的指令。记录裁判模型、
版本和规则用途；不要把凭据、rubric 或 Judge 输出返回给目标 Agent。

## 校准

在实际目标运行前至少检查：

| fixture | 预期 |
| --- | --- |
| 明确完成能力且证据充分 | 通过 |
| 表面合理但工具、状态或事实错误 | 不通过 |

针对已知风险再增加一个边界 fixture，例如接受不同但有效的回答，或拒绝目标输出中的 prompt
injection。若正确样本失败或错误样本通过，先修证据和 rubric，不要用更多弱代理分数掩盖问题。

## 失败语义

- Agent 输出错误、矛盾、无依据或未完成：明确 Score 为不通过；
- Judge 超时、结构化输出无效、证据缺失、Evaluator 崩溃或凭据失败：基础设施错误，不产生 Agent
  结论；
- 异步语义 Score 尚未生成：缺失状态，不等于失败；
- 重跑失败项时只选择最新 Score 明确不通过的当前 Dataset item，具体行为以
  `evals/README.md` 和现有 runner 为准。

## 运行后审计

逐个 Dataset item 关联检查：

1. 实际运行的 Dataset、item、profile、Provider 和工具暴露；
2. Agent response、Tool/Validator Trace、初始/最终状态和错误；
3. 每个确定性 Score 的 checks、value、comment 和 metadata；
4. 每个语义 Score 使用的关键证据、判定理由和结构化输出；
5. 所需 Score 是否完整，以及缺失是否来自异步或基础设施；
6. Agent 是否真正使用目标能力，而不是被环境、fixture 或 rubric 泄露答案；
7. 是否存在过度引用、虚构动作、满足代理指标或其他 reward hacking。
