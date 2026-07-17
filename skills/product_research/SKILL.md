---
name: product_research
description: Find product candidates, shopping options, and price comparisons through governed product and price tools.
enabled: true
disable-model-invocation: false
---
## Governed Tools
- product_search
- shopping_search
- price_compare

## Permissions
- tool:product_search
- tool:shopping_search
- tool:price_compare

## Required Inputs
- product_search: query, visual_summary
- shopping_search: query, visual_summary
- price_compare: query, items

## When To Use
- User asks to find products, compare offers, check prices, or choose what to buy.
- User asks product questions that need structured candidates, platforms, prices, or offers.
- User provides image or video product context and asks to find similar items or compare options.
- 用户询问购物、选品、比价、优惠、平台价格或“这是什么商品/哪里买”。

## When Not To Use
- User asks for general web news or non-shopping information; use web_search instead.
- User asks to analyze media content without shopping intent; use visual_understanding instead.
- User asks to generate a new image or 3D scene; use image_creation instead.

## Safe Examples
- compare white low-top sneakers under 500
- compare prices for these headphones
- 这张图里的杯子哪里可以买
- 帮我找同款并比价

## Runtime Constraints
- Use product_search first when no structured product candidates are already available.
- When calling price_compare after product_search, pass full structured item objects, not title strings.
- Use only governed product tools through ToolExecutor; do not scrape stores or call provider APIs directly.
- External provider behavior remains controlled by runtime profile and provider configuration.
