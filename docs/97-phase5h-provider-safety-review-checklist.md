# 97 Phase 5H Provider Safety Review Checklist

## 必须满足

- ProviderError taxonomy 已统一。
- ProviderSafetyPolicy 存在。
- 敏感信息脱敏策略存在。
- TimeoutPolicy 存在。
- RetryPolicy 存在。
- FallbackPolicy 存在。
- Mock fallback 默认关闭。
- ProviderCallBudget 存在。
- 每个 run 可记录 provider call count。
- 超出预算时返回结构化错误。
- Trace query API 或等价只读查询存在。
- Trace 不泄露 API Key / Authorization / Bearer token / base64 / raw provider response。
- Eval 有 provider_safety suite 或等价覆盖。
- 默认 pytest 不调用真实 Provider。
- 默认 eval 不调用真实 Provider。
- 不新增真实 Provider。
- 不写 API Key。
- 不提交真实 Provider 输出样本。

## 审计报告

最终生成：

```text
docs/98-phase5h-provider-safety-review.md
```

报告包含：

1. ProviderError taxonomy 状态。
2. SafetyPolicy 状态。
3. Retry / Fallback / Timeout 状态。
4. ProviderCallBudget 状态。
5. Trace Query 状态。
6. Redaction 状态。
7. Eval / API 覆盖。
8. 默认离线安全边界。
9. Phase 5I 建议。

## Phase 5I 建议方向

Phase 5H 后建议进入：

```text
Memory Hardening
```

不要在 Phase 5H 审计中直接实现 Memory Hardening、MCP 或 Skills。
