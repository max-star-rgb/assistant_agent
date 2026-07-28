---
name: langfuse-eval-engineering
description: Design, add, revise, run, or audit assistant_agent Langfuse-native Agent evaluations from repository context and optional authorized traces. Use for Dataset case selection, trace-informed regression cases, evaluation criteria, Code Evaluator or LLM-as-a-Judge design, Experiment review, Score completeness, and distinguishing Agent failures from evaluation infrastructure failures. Do not use for ordinary pytest work or real-service connectivity checks.
---

# Langfuse Eval Engineering

把仓库事实和经授权的 Trace 转化为现有 Langfuse Dataset、Experiment、Evaluator 和 Score。保留
Langfuse 为运行时评测权威；不要创建 Harbor task、第二套本地 case runner 或平行结果账本。

开始前完整读取 `evals/README.md`，以其中的当前目录、profile、Score、命令和安全边界为唯一事实权威。
若它与本 skill 不一致，按该文档和当前源码执行，并修正本 skill。

## 1. 分类请求

先判断问题属于哪一层：

- 确定性代码契约或回归测试：转到 `tests/`，并使用
  `$assistant-agent-development-testing`；
- 真实 Provider、Tool、Context 或 Memory 是否连通：转到 `evals/system/`；
- 模型选择、工具语义、多轮约束、回答质量或任务完成度：继续本流程。

不要把同一检查同时实现为 pytest、system eval 和 Langfuse case。

## 2. 映射 Agent

只读检查公开入口可达的 runtime、prompt、model、routing、tools、policy、memory、外部依赖、身份、
状态和副作用，并搜索现有 Dataset、Evaluator、测试、fixture 和已知失败。

在对话中总结：

```text
Agent: 目标与公开入口
Purpose: 用户和任务
Abilities: 预期能力
Tools and data: 工具、数据和依赖
Effects: 读取、写入和状态变化
Evidence: 测试、现有案例、失败或 Trace
```

映射期间不要启动真实服务、安装依赖或使用凭据。基于真实 run、通话或机器日志诊断时，先按
`AGENTS.md` 读取可对应问题的最新 `.data/**` 日志。

只有用户提供 Trace 来源或明确要求使用 Trace 时，才读取
[references/trace-sourcing.md](references/trace-sourcing.md)。

## 3. 提出评测方向

基于映射提出二到三个不同 capability：

```text
Name
Example request: 真实用户会提出的请求
Tests: 能区分的 Agent 行为
Needs: 主要数据、环境或评判证据
```

推荐一个方向并等待用户选择。不要在用户选择前修改 Dataset、Evaluator、runner 或运行真实评测。

## 4. 确认场景和运行边界

用户选择后读取 [references/case-design.md](references/case-design.md) 和
[references/verifier-audit.md](references/verifier-audit.md)，设计一个必须使用所选能力才能完成的
场景。优先调用现有 `AgentGraphRuntime` 和现有 Experiment 入口；只有既有入口无法表达时才提出薄适配，
且不得把工具选择、重试、推理或最终回答移入适配层。

实施前用不超过 150 字给出：

```text
Case: 请求与 capability
Dataset/profile: 目标 Dataset 与执行 profile
Dependencies: live、frozen 或 simulated；凭据、影响和数据来源
Success: 独立证据和对应评分层
Recommendation: 推荐边界及原因
```

等待用户批准或修订。真实 Provider、真实工具和写操作仍需遵守 `AGENTS.md` 与 `evals/README.md`
规定的显式开关；不得写入生产状态。

## 5. 实现一个 capability

每次只实现一个独立 capability：

1. 扩展现有 Langfuse Dataset seed、Experiment task、环境 fixture 或 Evaluator；不要新增第二套
   Dataset runner；
2. Dataset seed 只承担显式初始化或重置，Langfuse 中的 Dataset、Experiment、Evaluator 和 Score
   继续作为运行时权威；
3. 只把任务输入和目标运行需要的环境事实暴露给 Agent；不要暴露 Judge rubric、隐藏证据、裁判凭据
   或期望结论；
4. 对写操作使用可丢弃、可复位且可观察的本地状态；对动态只读数据记录来源和时间；
5. 让 Experiment 输出足以证明终态、Tool/Validator 链路、初始/最终状态和回答依据的结构化证据；
6. 若修改确定性代码或 pytest，使用 `$assistant-agent-development-testing` 选择最小充分验证；
7. 行为、命令或权威边界变化时，同步维护 `evals/README.md` 及直接相关文档。

不要通过关键词、正则或预设话术替 Agent 决定工具候选、工具选择或参数。

## 6. 校准、运行和审计

先用一个明确正确结果和一个可信但错误结果校准新增或修改的评判逻辑。随后执行
`evals/README.md` 指定的离线契约验证和 dry-run。只有用户批准且真实配置完整时，才运行真实
Experiment。

运行后同时检查：

- Agent 的输入、响应、Tool/Validator Trace、状态变化和终态；
- Code Evaluator 的客观检查与证据；
- LLM Judge 的输入证据、判定、理由、结构化输出和缺失情况；
- 每个 Dataset item 所需 Score 是否完整；
- resolved profile、Provider、工具暴露和隔离边界是否符合批准方案。

任务错误应得到明确不通过；Judge 超时、凭据错误、缺少证据、解析失败、Evaluator 崩溃或 Score
尚未生成属于评测基础设施状态，不得伪装成 Agent 通过或失败。

## 7. 复盘

向用户说明 case 路径、Dataset/profile、运行命令、能力边界、依赖策略、Agent 行为、评分结果、
基础设施状态和限制。请用户批准、修订、删除或选择下一个不同 capability。

## 不变量

- Langfuse 是 Agent case eval 的运行时权威。
- 一个 case 只验证一个可命名 capability；文案变化不构成新能力。
- Trace 用于发现请求和失败模式，不是正确答案。
- 工具行为以 Runtime/Trace/状态证据为准，不以 Agent 自述为准。
- pytest 保持 mock/local/offline；真实 Provider 不得静默回退 mock。
- 不提交凭据、原始生产 Trace、真实用户数据或评测运行生成物。
