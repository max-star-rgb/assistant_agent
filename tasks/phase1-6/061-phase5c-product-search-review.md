# Task 061 Phase 5C Review

## Goal

生成 Phase 5C 审计报告，确认 Product Search / Price Compare Provider Baseline 已完成。

## Read first

- `docs/60-phase5c-release-checklist.md`
- 当前 docs/
- 当前 tasks/
- 当前 src/
- 当前 tests/
- 当前 scripts/

## Requirements

生成：

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

允许小修：

- 更新 README 阶段状态。
- 补充文档链接。
- 更新 `.gitignore`。
- 删除缓存产物。

禁止：

- 接入真实电商平台。
- 写爬虫。
- 处理登录/cookie。
- 下单/支付。
- 写入 API Key。
- 大规模重构。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

## Stop condition

完成后停止，等待用户决定 Phase 5D。
