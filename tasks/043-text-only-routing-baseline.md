# Task 043 Text-only Routing Baseline

## Goal

确保纯文本输入可以正确路由到 direct_chat、image_generation、product_search、price_compare、memory_retrieval、render_3d，而不错误要求图片或视频。

## Read first

- `docs/43-direct-chat-and-text-only-capabilities.md`
- 当前 intent/router/planner
- 当前 eval cases

## Requirements

纯文本场景必须覆盖：

```text
direct_chat
image_generation
product_search
price_compare
memory_retrieval
render_3d
```

示例：

```text
帮我写一段商品介绍 → direct_chat
生成一张赛博朋克风格海报 → image_generation
帮我找 500 元以内的白色运动鞋 → product_search
比较一下 iPhone 15 和 iPhone 16 的价格 → price_compare 或 product_search → price_compare
上次那个黑色包还在吗 → memory_retrieval
把浅灰色沙发放到北欧风客厅看看 → render_3d
```

## Requirements

- 不得要求图片/视频。
- 不得误触发 image_understanding / video_understanding。
- 如果当前 image_generation/render 是 mock，也应正确路由。
- 不接入真实 Provider。

## Tests

新增或更新：

```text
tests/test_text_only_routing.py
```

至少覆盖上述场景。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 044。
