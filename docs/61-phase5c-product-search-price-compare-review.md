# 61 Phase 5C Review：Product Search / Price Compare Provider Baseline

## 审计结论

Phase 5C 已完成。当前系统已经把 `product_search` 和 `price_compare` 从早期 mock 工具推进到可替换 Provider、结构化 contract、可手动 smoke、可 eval/API 验证的轻量 baseline。

默认路径仍使用 Mock/Local 能力，不联网，不调用真实商品或价格 Provider。

## 1. Product Search 状态

已完成：

- `product_search` 支持 text-only query。
- `product_search` 支持接收 `visual_summary` / `video_summary` 作为搜索上下文。
- `ProductSearchAdapter` contract 已明确。
- 默认 `MockProductSearchAdapter` 返回确定性结构化商品结果。
- `LocalJsonProductSearchAdapter` 可读取小型本地 demo JSON。
- `HttpProductSearchAdapter` 只是 skeleton，不执行网络请求。
- 缺少 HTTP 配置时返回 `provider_unconfigured`。

关键文件：

- `src/multimodal_agent/services/product_adapter.py`
- `src/multimodal_agent/tools/product_search_tool.py`
- `tests/test_product_search_adapter.py`
- `tests/test_product_search_provider_selection.py`
- `tests/test_product_search_api.py`

## 2. Price Compare 状态

已完成：

- `price_compare` 支持接收 `product_search` 输出的 `ProductResult` 列表。
- `PriceCompareAdapter` contract 已明确。
- `MockPriceCompareAdapter` 输出结构化 offers 和 best offer。
- `LocalPriceCompareAdapter` 保持离线本地排序。
- `HttpPriceCompareAdapter` 只是 skeleton，不执行网络请求。
- 无候选商品时返回结构化 `price_no_products` 错误。
- 无符合预算/平台的报价时返回结构化 `price_no_offers` 错误。

关键文件：

- `src/multimodal_agent/services/product_adapter.py`
- `src/multimodal_agent/tools/price_compare_tool.py`
- `tests/test_price_compare_adapter.py`
- `tests/test_price_compare_tool.py`
- `tests/test_price_compare_api.py`

## 3. ProductResult / PriceOffer Contract

已完成的稳定对象：

```text
ProductSearchRequest
ProductResult
ProductSearchResult
PriceCompareRequest
PriceOffer
PriceCompareResult
RankingReason
```

`ProductResult` 保留旧字段 `url` / `similarity`，同时新增稳定字段：

```text
product_url
image_url
similarity_score
text_match_score
source
ranking_reason
brand/category/shop/rating/sales/material/color/style_tags
```

`PriceOffer` 稳定包含：

```text
offer_id
product_id
title
platform
price
currency
total_price
product_url
availability
rating
similarity_score
ranking_reason
```

关键测试：

- `tests/test_product_result_contracts.py`
- `tests/test_product_multistep_data_flow.py`
- `tests/contracts/test_product_adapter_contract.py`

## 4. Provider 边界

当前 Provider 边界：

- `mock`：默认运行和默认测试路径。
- `local_json`：只读小型本地 demo JSON，不联网。
- `local`：本地 price compare 排序，不联网。
- `http`：仅 skeleton，缺配置返回 `provider_unconfigured`，配置完整也返回 `provider_unavailable`，不会发起 HTTP 请求。

Phase 5C 没有接入真实电商平台，也没有实现爬虫、登录、cookie、购买、下单或支付。

## 5. Smoke 能力

已完成手动 smoke 入口：

```bash
python scripts/smoke_product_search.py --query "500 元以内的白色运动鞋"
python scripts/smoke_price_compare.py --query "白色运动鞋" --budget-max 500
```

local_json 示例：

```bash
MULTIMODAL_AGENT_PRODUCT_PROVIDER=local_json \
PRODUCT_SEARCH_LOCAL_PATH=demo_data/products/products.example.json \
python scripts/smoke_product_search.py --query "白色运动鞋"
```

安全状态：

- import 脚本不会触发 Provider。
- 默认 mock 可运行。
- local_json 使用小型本地 demo 数据。
- HTTP 缺配置时清晰提示。
- 默认 pytest 不联网。

关键文件：

- `scripts/smoke_product_search.py`
- `scripts/smoke_price_compare.py`
- `demo_data/products/products.example.json`
- `tests/test_product_search_smoke_scripts.py`
- `tests/test_price_compare_smoke_scripts.py`

## 6. Eval 覆盖

当前 eval 共 57 条，Phase 5C 已覆盖：

- text-only `product_search`
- text-only `price_compare`
- image/video summary → `product_search`
- `product_search -> price_compare` 多步链路
- media search compare 链路

最近验收结果：

```text
python scripts/run_evals.py
57 passed / 57 total
```

默认 eval 离线运行，不调用真实 Provider。

关键文件：

- `tests/evals/eval_cases.json`
- `scripts/run_evals.py`
- `tests/test_product_search_evals.py`

## 7. 安全边界

已确认：

- 未写入 API Key。
- 未创建包含真实密钥的 `.env` 或 `.env.local`。
- `.env` / `.env.*` 被忽略，`.env.example` 只包含占位符。
- 未提交大规模真实商品数据。
- `demo_data/products/products.example.json` 是小型本地示例数据，不包含用户隐私或真实 Provider raw response。
- API 输出不包含 `raw` / `provider_response`。
- 不执行购买、下单、支付。
- 不处理登录态、cookie 或反爬。
- 默认测试和 eval 不联网。

## 8. Phase 5D 建议

建议 Phase 5D 进入：

```text
Render 3D Capability Baseline
```

建议保持同样边界：

- 只做 adapter contract、mock/local provider、smoke、eval/API 覆盖。
- 不接真实渲染农场。
- 不引入重型 3D 依赖。
- 不处理支付或外部商业平台。

也可以选择先做一个小型 Provider Hardening 阶段，但不建议同时推进 Vision hardening、Harness Engineering 和真实电商接入。
