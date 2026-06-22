# Task 044 Media-aware Routing Baseline

## Goal

明确图片/视频存在时如何路由，避免“有媒体就只做理解”的错误。

## Read first

- `docs/42-assistant-capability-routing-baseline.md`
- 当前 media input schema
- 当前 intent/router/planner

## Requirements

必须覆盖：

```text
图片 + 看看图里有什么 → image_understanding
视频 + 总结这个视频 → video_understanding
图片 + 用这张图生成海报 → image_understanding → image_generation
图片 + 找同款并比价 → image_understanding → product_search → price_compare
视频 + 找视频里的商品 → video_understanding → product_search
图片 + 普通聊天文本 → 根据文本意图，不一定执行 image_understanding
```

## Routing principles

- 文本意图优先。
- 媒体是上下文，不是唯一意图。
- 缺媒体但请求理解图片/视频时，应 ask_followup。
- 有媒体但意图不明确时，应 ask_followup。

## Tests

新增或更新：

```text
tests/test_media_aware_routing.py
```

覆盖图片、视频、文本+媒体、多步媒体任务。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 045。
