# 81 Intent Router Eval Comparison

## 目标

建立可对比的 intent router eval，使项目可以评估：

```text
rule
mock_llm
hybrid
```

不同 router 的效果。

默认不调用真实 LLM。

## Eval 维度

```text
intent_accuracy
capability_accuracy
tool_selection_accuracy
ordered_tool_match
followup_accuracy
unexpected_tool_rate
missing_input_detection_rate
plan_step_accuracy
```

## Router 模式

### rule

当前默认模式。

```text
MULTIMODAL_AGENT_INTENT_ROUTER=rule
```

### mock_llm

使用 MockLLMIntentRouter，离线模拟 LLM 输出。

```text
MULTIMODAL_AGENT_INTENT_ROUTER=mock_llm
```

### hybrid

规则高置信时直接通过，低置信时调用 mock_llm。

```text
MULTIMODAL_AGENT_INTENT_ROUTER=hybrid
```

## Eval Case 扩展

新增模糊/复杂案例：

```text
帮我把这个东西弄得更适合卖
看看这个有没有便宜点的，顺便做个图
按我之前喜欢的风格，给这个做个展示
这个能不能放到客厅里看看
帮我处理一下这个商品，让它更有吸引力
```

## Runner 建议

扩展：

```bash
python scripts/run_evals.py --router rule
python scripts/run_evals.py --router mock_llm
python scripts/run_evals.py --router hybrid
```

或输出 router_mode 字段。

## 默认安全边界

- 默认 router 仍为 rule。
- mock_llm 不调用外部服务。
- hybrid 默认使用 mock_llm。
- 真实 LLM router 不参与默认 eval。
- 不调用真实 Provider。

## 验收标准

- Eval 可以比较 rule / mock_llm / hybrid。
- 模糊 case 有覆盖。
- 缺输入 case 有覆盖。
- 输出 failed_case_ids。
- 默认离线。

## 当前实现

当前实现落点：

```text
scripts/run_evals.py
tests/evals/eval_cases.json
```

Runner 支持：

```bash
python scripts/run_evals.py --router rule
python scripts/run_evals.py --router mock_llm
python scripts/run_evals.py --router hybrid
```

默认仍是：

```bash
python scripts/run_evals.py --router rule
```

`rule` 模式继续运行完整离线 AgentWorkflow。`mock_llm` 和 `hybrid` 模式只运行
IntentRouterAdapter 的离线 decision 对比，不执行工具、不调用真实 LLM、不调用真实 Provider。

输出新增：

- `router_mode`
- `routers`
- 保留 `failed_case_ids`

新增的 `router_comparison` eval cases 带有 `router_expectations`，用于为
`mock_llm` / `hybrid` 指定独立期望，不破坏默认 rule eval。
