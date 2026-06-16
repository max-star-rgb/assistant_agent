# 82 Phase 5F Review Checklist

## 必须满足

- IntentDecision schema 已统一。
- Rule Router 输出 confidence、matched_rules、reason。
- CapabilityValidator 已接入。
- 缺图片不会执行 image_understanding。
- 缺视频不会执行 video_understanding。
- 缺场景不会执行 render_3d。
- price_compare 无商品但有 query 时可规划 product_search。
- LLM Intent Router Adapter skeleton 存在。
- MockLLMIntentRouter 可离线测试。
- 默认 router 仍为 rule。
- Hybrid router 只在配置启用时使用。
- LLM 输出不能直接执行工具。
- 所有 LLM 输出经过 schema 校验和 CapabilityValidator。
- Planner slot filling 有测试。
- Eval 可比较 rule / mock_llm / hybrid。
- 默认 pytest / eval 不调用真实 LLM 或真实 Provider。

## 审计报告

最终生成：

```text
docs/83-phase5f-hybrid-intent-router-review.md
```

报告包含：

1. IntentDecision 状态。
2. Rule Router confidence 状态。
3. CapabilityValidator 状态。
4. LLM Router Adapter 状态。
5. Planner / Slot Filling 状态。
6. Eval comparison 状态。
7. 默认离线安全边界。
8. 仍然存在的问题。
9. Phase 5G 建议。

## Phase 5G 建议方向

Phase 5F 后可考虑：

```text
Provider Safety / Retry / Cost / Trace Query
Memory Hardening
MCP / Skills Packaging
```

不要在 Phase 5F 审计中直接实现这些内容。
