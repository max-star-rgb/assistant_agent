# Task 007：商品搜索与比价

## Goal

实现商品搜索和比价 mock adapter，使 Agent 能完成“找相似款并比较价格”的流程。

## Read first

- `docs/05-tool-contracts.md`
- `docs/04-intent-and-routing.md`

## Scope

新增/修改：

```text
src/multimodal_agent/services/product_adapter.py
src/multimodal_agent/tools/product_search_tool.py
src/multimodal_agent/tools/price_compare_tool.py
tests/unit/test_product_search_compare.py
```

## Steps

1. 定义 `ProductSearchAdapter`。
2. 实现 mock 搜索，返回至少 3 个 ProductResult。
3. 实现比价工具，按价格升序排序。
4. 每个商品包含 title、price、shop、similarity_score、reason。

## Acceptance

```bash
pytest tests/unit/test_product_search_compare.py
```

必须验证：

- 能根据“白色低帮运动鞋”返回商品。
- 比价结果价格升序。
- 每个结果包含推荐理由。

## Out of scope

- 不调用真实电商 API。
- 不实现购买/下单。
