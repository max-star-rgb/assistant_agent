请审计当前 Phase 5F 完成情况，不要先改代码。

请回答：

1. IntentDecision schema 是否统一？
2. Rule Router 是否输出 confidence / matched_rules / reason？
3. CapabilityValidator 是否接入执行前校验？
4. 缺图片/视频/场景时是否进入 ask_followup？
5. LLM Intent Router Adapter 是否 default-off？
6. MockLLMIntentRouter 是否可离线测试？
7. LLM 输出是否经过 schema 校验和 Validator？
8. Planner slot filling 是否改进？
9. Eval 是否能比较 rule / mock_llm / hybrid？
10. 默认 pytest / eval 是否不调用真实 LLM 或 Provider？
11. 下一步应该执行哪个 task？
