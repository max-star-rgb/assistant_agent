# 79 LLM Intent Router Adapter

## 目标

增加可选 LLM Intent Router Adapter，但默认关闭，不调用真实 LLM。

LLM Router 的作用是：

```text
处理规则低置信、模糊表达、多意图、复杂自然语言
```

它不是执行器，也不是工具调用器。

## 推荐链路

```text
UserRequest
  ↓
RuleIntentRouter
  ↓ low confidence
LLMIntentRouterAdapter
  ↓
IntentDecision
  ↓
CapabilityValidator
  ↓
Planner / LangGraph
```

## 配置

建议：

```text
MULTIMODAL_AGENT_INTENT_ROUTER=rule|hybrid|llm
```

默认：

```text
rule
```

测试默认：

```text
rule
mock_llm
hybrid_mock
```

真实 LLM 必须显式启用。

## Adapter 接口

```python
class IntentRouterAdapter(Protocol):
    def decide(self, request: IntentRouterRequest) -> IntentDecision:
        ...
```

## IntentRouterRequest

建议字段：

```text
user_query
has_image
has_video
has_audio
memory_context
available_capabilities
current_state_summary
```

## IntentDecision 输出

LLM 必须输出结构化 IntentDecision。

不允许自由文本直接控制工具执行。

## MockLLMIntentRouter

必须提供 MockLLMIntentRouter，用于离线测试：

```text
输入模糊案例
输出预设 IntentDecision
```

## 真实 LLM Router Skeleton

可以预留：

```text
OpenAICompatibleIntentRouter
QwenIntentRouter
```

但默认不启用，不自动安装依赖，不调用真实 API。

## Prompt 原则

LLM Router prompt 应明确：

- 只输出 IntentDecision JSON。
- 不直接执行工具。
- 不编造工具。
- 不绕过 missing input。
- 不请求 API Key。
- 不输出 provider raw response。
- 不输出用户敏感信息。

## Validator 必须存在

即使 LLM 输出看似合理，也必须经过 CapabilityValidator。

## 验收标准

- 默认 router 仍为 rule。
- mock_llm router 可离线测试。
- hybrid router 可在低置信时调用 mock_llm。
- LLM 输出经过 schema 校验。
- LLM 输出经过 CapabilityValidator。
- 真实 LLM 不默认调用。

## 当前实现

当前实现文件：

```text
src/multimodal_agent/schemas/intent_router.py
src/multimodal_agent/agent/intent_router_adapter.py
```

已提供：

- `IntentRouterRequest`
- `IntentRouterAdapter` Protocol
- `RuleIntentRouterAdapter`
- `MockLLMIntentRouter`
- `HybridIntentRouterAdapter`
- `OpenAICompatibleIntentRouter` skeleton
- `create_intent_router_adapter()`

配置项：

```text
MULTIMODAL_AGENT_INTENT_ROUTER=rule|mock_llm|hybrid|llm
```

默认值仍是 `rule`。`mock_llm` 与 `hybrid` 只使用本地 mock 逻辑，不访问网络。
`llm` 当前只是 default-off skeleton，会返回结构化 fallback decision，不调用真实 LLM。

所有 mock / skeleton 输出都会先 parse 为 `IntentDecision`，再经过
`CapabilityValidator`。LLM Router 不具备工具执行权。
