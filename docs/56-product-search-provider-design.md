# 56 Product Search Provider Design

## 目标

让 `product_search` 能力从 Mock-only 升级为可替换 Provider 的结构。

## 能力定义

`product_search` 用于：

```text
文本商品搜索
同款搜索
相似款搜索
基于视觉摘要搜索商品
基于视频摘要搜索商品
根据预算、品牌、风格、平台等条件搜索
```

## 推荐链路

```text
AgentGraphRuntime
  ↓
ProductSearchTool
  ↓
ProductSearchAdapter
  ↓
MockProductSearchAdapter / LocalJsonProductSearchAdapter / HttpProductSearchAdapter
```

## ProductSearchRequest

建议字段：

```text
query
visual_summary optional
video_summary optional
objects optional
colors optional
materials optional
brand optional
category optional
budget_min optional
budget_max optional
platforms optional
top_k
user_id
session_id
memory_context optional
```

## ProductSearchResult

建议字段：

```text
items: list[ProductResult]
provider
query_used
filters_used
total
errors
latency_ms optional
```

## ProductResult

基础字段：

```text
product_id
title
brand optional
category optional
price optional
currency
platform
shop optional
image_url optional
product_url optional
similarity_score optional
text_match_score optional
reason
source
```

## 默认 Provider

默认必须使用：

```text
MockProductSearchAdapter
```

可以新增：

```text
LocalJsonProductSearchAdapter
```

用于本地 demo，不联网。

## 真实 Provider Skeleton

可以预留：

```text
HttpProductSearchAdapter
```

配置项：

```text
MULTIMODAL_AGENT_PRODUCT_PROVIDER=mock|local_json|http
PRODUCT_SEARCH_BASE_URL=
PRODUCT_SEARCH_API_KEY=
PRODUCT_SEARCH_TIMEOUT_SECONDS=
```

默认不启用真实 HTTP Provider。

## 错误码

```text
provider_unconfigured
provider_timeout
provider_bad_response
provider_auth_failed
provider_rate_limited
provider_unavailable
product_query_empty
product_no_results
```

## 验收标准

- 文本商品搜索不要求图片/视频。
- 图片/视频摘要可作为搜索上下文。
- 默认测试不联网。
- Provider 缺配置时返回结构化错误。
- Tool 不直接调用 HTTP / SDK。
