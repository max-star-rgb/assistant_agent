# 59 Product Search / Price Compare Smoke and Safety

## 目标

为商品搜索和比价提供手动 smoke 入口，但默认不调用真实 Provider，不联网。

## Smoke 脚本建议

```text
scripts/smoke_product_search.py
scripts/smoke_price_compare.py
```

## Product Search Smoke

默认 mock：

```bash
python scripts/smoke_product_search.py --query "500 元以内的白色运动鞋"
```

local_json：

```bash
export MULTIMODAL_AGENT_PRODUCT_PROVIDER=local_json
export PRODUCT_SEARCH_LOCAL_PATH=demo_data/products/products.example.json
python scripts/smoke_product_search.py --query "白色运动鞋"
```

`PRODUCT_SEARCH_LOCAL_JSON` is also accepted as a backward-compatible alias for manual smoke runs.

http provider：

```bash
export MULTIMODAL_AGENT_PRODUCT_PROVIDER=http
export PRODUCT_SEARCH_BASE_URL="<local-or-private-service>"
export PRODUCT_SEARCH_API_KEY="<local-only>"
python scripts/smoke_product_search.py --query "白色运动鞋"
```

## Price Compare Smoke

```bash
python scripts/smoke_price_compare.py --query "白色运动鞋" --budget-max 500
```

可以先内部执行：

```text
product_search → price_compare
```

## 本地 Demo Data

允许提交小型示例商品数据：

```text
demo_data/products/products.example.json
```

要求：

- 不包含真实 API Key。
- 不包含用户隐私。
- 不包含大规模爬取数据。
- 只用于 demo 和测试。
- 数据量小，字段结构清晰。

## 安全要求

禁止：

- 爬虫。
- 登录态 cookie。
- 下单/支付。
- 默认打开商品 URL。
- 提交真实 provider raw response。
- 默认联网。

## 验收标准

- smoke 脚本 import 不触发 Provider。
- 默认 mock smoke 可运行。
- local_json smoke 可运行。
- 缺真实 Provider 配置时清晰提示。
- 默认 pytest 不调用真实 Provider。
