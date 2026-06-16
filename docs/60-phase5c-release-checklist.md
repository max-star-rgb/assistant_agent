# 60 Phase 5C Release Checklist

## 必须满足

- product_search 支持 text-only query。
- product_search 可接收 visual_summary / video_summary。
- price_compare 可接收 product_search 结果。
- ProductSearchAdapter contract 明确。
- PriceCompareAdapter 或 compare contract 明确。
- ProductResult / PriceOffer schema 稳定。
- 默认 provider 仍为 mock/local。
- 默认 pytest 不联网。
- 默认 eval 不调用真实 Provider。
- smoke 脚本必须用户显式运行才触发真实 Provider。
- local_json demo 数据不包含敏感信息。
- API 输出不暴露 provider raw response。
- 不执行购买、下单、支付。

## 检查命令

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

## Phase 5C 审计报告

最终生成：

```text
docs/61-phase5c-product-search-price-compare-review.md
```

报告包含：

1. Product Search 状态。
2. Price Compare 状态。
3. ProductResult / PriceOffer contract。
4. Mock / local_json / http Provider 边界。
5. Smoke 能力。
6. Eval 覆盖。
7. 安全边界。
8. Phase 5D 建议。
