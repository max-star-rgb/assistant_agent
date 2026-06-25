# Phase 8A.2：ReAct Final Answer Handoff Fix

## 背景

在真实 chat provider 驱动的 assistant loop 中，LLM 会按以下路径工作：

```text
Decision Reason -> Action -> Observation -> Final Answer
```

`Decision Reason` 只对应公开结构化字段 `AssistantDecision.reason`，用于记录高层决策理由；不展示完整 chain-of-thought、`Thought:` 内容或思维链。

当前实现中，LLM 在工具 observation 之后返回 `final_answer` 时，assistant 节点只把状态标记为完成，没有把该回答写入 `AgentState.response`。随后图继续进入 `compose_response`，本地响应合成器会根据 mock/local 工具结果重新生成最终文本，导致真实 LLM 的最终回答被覆盖。

典型表现是：DeepSeek 已经根据 observation 给出解释，但终端最后显示的是 mock 商品或 mock 工具结果的模板回答。

## 目标

真实 assistant loop 中，LLM 基于 observation 给出的 `final_answer` 必须成为最终响应。

本地 composer 仍保留为 fallback：

```text
mock/offline rule plan -> composer
real LLM final_answer with message -> state.response
no assistant final answer -> composer
```

## 边界

- 不新增真实 Provider。
- 不调用真实外部 API。
- 不修改 tool registry 聚合导出。
- 不改变 mock/offline demo 的确定性合成路径。
- 不引入 planning/reflection。

## 响应数据要求

保留 assistant final answer 时，响应数据至少包含：

```text
final_answer_source
assistant_decision
reason
iterations
tool_count
tool_observations
contracts
output_refs
errors
provider_budget
```

这样 demo 和 trace 可以同时看到：

```text
assistant 的最终回答
已执行的工具序列
工具 observation / contract
```

## 测试清单

1. 真实风格 scripted chat adapter 在工具调用后返回 `final_answer`，最终 response 必须等于该回答。
2. 最终 response 不应被 mock/local 工具结果模板覆盖。
3. mock/offline rule plan 仍交给 composer 生成结构化响应。
4. demo 脚本能展示 `final_answer_source`，便于确认交接路径。
