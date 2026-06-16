# 83 Phase 5F Hybrid Intent Router Review

## 结论

Phase 5F Hybrid Intent Router & Planner Quality 已完成。当前阶段只改进意图决策结构、规则路由可解释性、可选 mock LLM router skeleton、planner slot filling 和离线 eval 对比；没有默认调用真实 LLM，没有接入新的真实 Provider，没有实现 MCP / Skills，也没有允许 LLM 直接执行工具。

默认运行路径仍以 rule router 为主。`mock_llm` 和 `hybrid` 只作为离线可测的 router comparison 模式存在。

## 1. IntentDecision 状态

统一 intent decision schema 已落地：

```text
src/multimodal_agent/schemas/intent_decision.py
```

当前包含：

- `IntentDecision`
- `PlanStep`
- `DecisionSource`

`PlanStep.tool_name` 会校验 capability contract 中声明的工具名，防止 LLM 或 mock router 直接指定任意工具。`IntentDecision` 支持 `primary_intent`、`capabilities`、`plan_steps`、`missing_inputs`、`confidence`、`source`、`reason`、`matched_rules` 和 `raw_output_ref`。

## 2. Rule Router Confidence 状态

规则路由新增结构化入口：

```text
IntentDetector.detect_decision()
```

该入口返回 `IntentDecision`，并包含：

- `source="rule"`
- `confidence`
- `matched_rules`
- `reason`
- `plan_steps`
- `missing_inputs`

旧入口 `IntentDetector.detect()` 仍保留，用于兼容默认 runtime 和历史 eval。规则高置信决策可直接通过，低置信决策为后续 hybrid fallback 提供入口。

## 3. CapabilityValidator 状态

执行前安全闸门已落地：

```text
src/multimodal_agent/agent/capability_validator.py
```

Validator 负责：

- 缺图片时不允许执行 `image_understanding`，改为 `ask_followup`。
- 缺视频时不允许执行 `video_understanding`，改为 `ask_followup`。
- 缺渲染场景时不允许执行 `render_3d`，改为 `ask_followup`。
- `price_compare` 无商品候选但有 query 时补为 `product_search -> price_compare`。
- LLM/mock 输出必须先 parse 为 `IntentDecision`，再经过 Validator。

Validator 不调用工具、不访问网络、不调用真实 Provider。

## 4. LLM Router Adapter 状态

可选 LLM router skeleton 已落地：

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
- `OpenAICompatibleIntentRouter` default-off skeleton
- `create_intent_router_adapter()`

配置项：

```text
MULTIMODAL_AGENT_INTENT_ROUTER=rule|mock_llm|hybrid|llm
```

默认仍是 `rule`。`mock_llm` 和 `hybrid` 只使用本地 mock 逻辑。`llm` 当前只返回 default-off fallback decision，不调用真实 LLM。

## 5. Planner / Slot Filling 状态

Planner quality 已增强：

```text
src/multimodal_agent/schemas/planning.py
src/multimodal_agent/agent/planner.py
```

`TaskStep` 已支持：

- `depends_on`
- `input_refs`
- `required_inputs`
- `optional`
- `reason`

`RuleBasedTaskPlanner` 已支持：

- query-only `price_compare` 自动补 `product_search -> price_compare`。
- `memory_retrieval -> image_generation`。
- `image_understanding -> product_search -> price_compare -> image_generation`。
- `product_search -> render_3d`。
- 图片理解缺图片、视频理解缺视频、渲染缺场景时进入 follow-up。

Planner 仍只生成计划，不调用工具、不调用真实 LLM、不调用真实 Provider。

## 6. Eval Comparison 状态

Eval runner 已支持 router 对比：

```text
scripts/run_evals.py
tests/evals/eval_cases.json
```

可运行：

```bash
python scripts/run_evals.py
python scripts/run_evals.py --router rule
python scripts/run_evals.py --router mock_llm
python scripts/run_evals.py --router hybrid
```

默认仍是 `rule`，并运行完整离线 AgentWorkflow。`mock_llm` 和 `hybrid` 模式只运行离线 IntentRouterAdapter decision 对比，不执行工具。输出包含 `router_mode`、`routers` 和 `failed_case_ids`。

新增 `router_comparison` eval cases 覆盖模糊、多意图和缺输入表达。

## 7. 默认离线安全边界

当前安全边界：

- 默认 router 是 `rule`。
- 默认 pytest 离线运行。
- 默认 eval 离线运行。
- 默认 demo runner 离线运行。
- `mock_llm` 不访问网络。
- `hybrid` 只在配置启用时使用 mock fallback。
- `llm` 是 default-off skeleton，不调用真实 LLM。
- LLM/mock 输出不能直接执行工具。
- 所有 LLM/mock 输出必须经过 schema 和 CapabilityValidator。
- 未写入 API Key。
- 未接入新的真实 Provider。
- 未实现 MCP / Skills。

## 8. 仍然存在的问题

- 默认 runtime 仍通过旧 `IntentDetector.detect()` 兼容路径工作，`detect_decision()` 尚未成为默认执行链路。
- `mock_llm` 只是离线行为模拟，不代表真实 LLM routing 质量。
- `hybrid` comparison 当前主要用于指标观察，尚未作为默认生产路由。
- Planner 仍是规则式 slot filling，没有复杂语义解析或学习能力。
- 真实 LLM router 的 prompt、成本控制、重试、超时和 trace 查询未实现。
- Memory 质量、长期偏好合并和上下文压缩仍待后续阶段强化。

## 9. 后续阶段建议

原 Phase 5F 审计曾建议下一阶段聚焦 Provider Safety / Retry / Cost / Trace Query。当前阶段规划已调整：Phase 5G 先补齐 `video_understanding` 作为外部 Video MLLM capability 的轻量 baseline。

Phase 5G 应聚焦：

- 定义 `VideoUnderstandingRequest` / `VideoUnderstandingResult`。
- 定义 `VideoUnderstandingAdapter` contract。
- 保持默认 `MockVideoUnderstandingAdapter`。
- 预留真实 Video Provider 的 default-off skeleton。
- 支持视频理解结果进入后续 Agent 能力链路。

Provider Safety / Retry / Cost / Trace Query 可顺延到 Phase 5H 或后续阶段：

- 为真实 Provider 增加统一超时、重试和熔断策略。
- 建立 provider cost / token / latency 观测字段。
- 增强 trace 查询和调试能力。
- 继续强化敏感信息过滤，避免 API Key、Authorization、Bearer token、base64 或 raw provider response 出现在日志和错误中。
- 保持默认测试离线，真实 Provider 仍只允许 env-gated integration tests 或用户手动 smoke。

后续阶段不应回填到 Phase 5F，也不应在没有明确任务时实现 MCP / Skills。

## 验收快照

本审计任务的验收命令：

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_evals.py --router rule
python scripts/run_evals.py --router mock_llm
python scripts/run_evals.py --router hybrid
git status --short
```

验收结果以任务执行输出为准。`mock_llm` / `hybrid` 的 `failed_case_ids` 是 router comparison 指标，不表示命令执行失败。
