---
name: image_creation
description: Generate images, product visuals, visual assets, or 3D scene previews through governed image_generation and render_3d tools.
enabled: true
disable-model-invocation: false
---
## Governed Tools
- image_generation
- render_3d

## Permissions
- tool:image_generation
- tool:render_3d

## Required Inputs
- image_generation: prompt, product
- render_3d: prompt, product

## When To Use
- User asks to create, generate, render, visualize, or produce a new image.
- User asks for product visuals, concept art, mockups, marketing images, or visual variants.
- User explicitly asks for 3D render, scene preview, model-like visualization, or placement in a scene.
- 用户要求生成图片、海报、素材图、产品效果图、3D 渲染或场景预览。

## When Not To Use
- User asks to inspect an existing image or video; use visual_understanding instead.
- User asks to find products or compare prices; use product_research instead.
- User asks for text-only explanation, search, or memory operations.

## Safe Examples
- generate a product hero image
- create four visual concepts for this item
- render this chair in a living room scene
- 生成一张商品主图

## Runtime Constraints
- Treat image_generation and render_3d as terminal artifact-producing tools.
- If multiple images are needed, request them in one image_generation call when supported instead of repeated calls.
- Use render_3d only for explicit render, 3D, model, or scene-preview intent.
- Validate returned artifact references before using them in final responses.
