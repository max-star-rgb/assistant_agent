# Task 055 Phase 5C Product Search / Price Compare Roadmap

## Goal

确认 Phase 5C 只聚焦 product_search 和 price_compare，不扩展 3D render、Vision hardening、Harness 等方向。

## Read first

- `docs/55-phase5c-product-search-price-compare-roadmap.md`
- `docs/47-phase5a-assistant-routing-review.md`
- `docs/54-phase5b-text-first-capabilities-review.md`
- 当前 README / docs index

## Scope

只更新文档和阶段说明，不做业务代码大改。

## Requirements

- 明确 Phase 5C 目标为 Product Search / Price Compare Provider Baseline。
- 明确默认 mock/local，不联网。
- 明确不做爬虫、不登录、不下单、不支付。
- 明确 text-only search 和 vision/video summary search 都支持。
- 不调用真实 Provider。
- 不写入 API Key。

## Suggested files

```text
docs/55-phase5c-product-search-price-compare-roadmap.md
tasks/README_PHASE5C.md
README.md
```

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 056。
