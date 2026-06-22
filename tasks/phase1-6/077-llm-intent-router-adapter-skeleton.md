# Task 077 LLM Intent Router Adapter Skeleton

## Goal

新增可选 LLM Intent Router Adapter skeleton 和 MockLLMIntentRouter，默认关闭，不调用真实 LLM。

## Read first

- `docs/79-llm-intent-router-adapter.md`
- 当前 ProviderConfig
- 当前 intent/router/planner
- 当前 eval runner

## Requirements

- 定义 IntentRouterAdapter Protocol。
- 定义 IntentRouterRequest schema。
- 新增 MockLLMIntentRouter。
- 可预留 OpenAI/Qwen compatible skeleton，但默认不启用。
- 配置 `MULTIMODAL_AGENT_INTENT_ROUTER=rule|mock_llm|hybrid|llm` 或等价机制。
- 默认必须为 rule。
- LLM 输出必须 parse 为 IntentDecision。
- LLM 输出必须经过 CapabilityValidator。
- 不调用真实 LLM。
- 不写 API Key。

## Tests

新增或更新：

```text
tests/test_llm_intent_router_adapter.py
tests/test_mock_llm_intent_router.py
tests/test_intent_router_provider_selection.py
```

覆盖：

- default rule。
- mock_llm output。
- hybrid calls mock_llm only on low confidence。
- malformed mock output → structured error or fallback。
- no real LLM call.

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 078。
