# 58 Product Result and Ranking Contracts

## 目标

统一商品搜索与比价的输入输出，保证后续 image_generation、render_3d、memory 等能力都能复用商品结果。

## 核心 Schema

建议统一以下对象：

```text
ProductSearchRequest
ProductResult
ProductSearchResult
PriceCompareRequest
PriceOffer
PriceCompareResult
RankingReason
```

## ProductResult 设计原则

ProductResult 不应绑定某个平台的原始字段。

必须稳定包含：

```text
product_id
title
price
currency
platform
image_url
product_url
similarity_score
reason
source
```

允许可选：

```text
brand
category
shop
rating
sales
material
color
style_tags
```

## RankingReason

推荐字段：

```text
score
factors
explanation
```

示例：

```json
{
  "score": 0.86,
  "factors": {
    "visual_similarity": 0.8,
    "price_match": 0.9,
    "text_match": 0.88
  },
  "explanation": "价格在预算内，视觉相似度较高，标题匹配白色低帮运动鞋。"
}
```

## 与多步任务的关系

### 图片找同款并比价

```text
image_understanding
  ↓
ProductSearchRequest.visual_summary
  ↓
product_search
  ↓
PriceCompareRequest.products
  ↓
price_compare
```

### 搜索后生成海报

```text
product_search
  ↓
ProductResult
  ↓
image_generation prompt_builder
```

### 搜索后渲染

```text
product_search
  ↓
ProductResult.image_url / product_url
  ↓
render_3d
```

## 输出安全

- product_url 必须是字符串，不自动打开。
- 不执行购买行为。
- 不处理支付。
- 不保存用户敏感交易信息。
- 不提交真实商品 API response raw dump。

## 验收标准

- Schema 可单测。
- Tool 输出符合 schema。
- API 输出稳定。
- Eval 能检查 product_search / price_compare 工具顺序。
