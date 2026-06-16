# 57 Price Compare Provider Design

## 目标

让 `price_compare` 能力支持对商品候选进行结构化比价，并为真实价格 Provider 做好扩展边界。

## 能力定义

`price_compare` 用于：

```text
比较多个商品候选价格
同款多平台比价
按预算筛选
按平台/评分/相似度排序
输出最优购买建议
```

## 推荐链路

```text
AgentGraphRuntime
  ↓
PriceCompareTool
  ↓
PriceCompareAdapter 或 ProductSearchAdapter.compare()
  ↓
MockPriceCompareAdapter / LocalJsonPriceCompareAdapter / HttpPriceCompareAdapter
```

如果当前项目已有 `ProductSearchAdapter.compare()`，Phase 5C 可以先保持兼容，但建议逐步抽出独立 PriceCompareAdapter contract。

## PriceCompareRequest

建议字段：

```text
query optional
products: list[ProductResult]
budget_min optional
budget_max optional
platforms optional
sort_by: price|similarity|rating|value
currency
top_k
user_id
session_id
```

## PriceCompareResult

建议字段：

```text
offers: list[PriceOffer]
best_offer optional
ranking_reason
provider
errors
latency_ms optional
```

## PriceOffer

建议字段：

```text
offer_id
product_id
title
platform
shop optional
price
currency
shipping_fee optional
total_price optional
product_url optional
availability optional
rating optional
sales optional
similarity_score optional
reason
```

## 排序策略

默认本地排序即可：

1. 过滤无价格商品。
2. 计算 total_price。
3. 根据预算过滤。
4. 优先相似度高且价格合理。
5. 输出 best_offer 和 reason。

## 错误码

```text
price_no_products
price_no_offers
provider_unconfigured
provider_timeout
provider_bad_response
provider_rate_limited
```

## 验收标准

- 没有候选商品时返回结构化错误，而不是崩溃。
- 有搜索结果时可继续比价。
- 默认 mock/local。
- 不联网。
- API 输出不泄露 provider raw response。
