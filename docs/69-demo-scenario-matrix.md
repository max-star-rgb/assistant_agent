# 69 Demo Scenario Matrix

## 目标

定义一组可演示、可复现、默认离线运行的端到端 demo scenarios，覆盖 Assistant Agent 的核心能力组合。

场景矩阵文件：

```text
demo_data/scenarios/e2e_demo_scenarios.json
```

## 场景字段

每个 scenario 必须包含：

```text
scenario_id
title
user_query
metadata
expected_tools
expected_response_contains
```

字段含义：

| 字段 | 含义 |
| --- | --- |
| `scenario_id` | 稳定唯一 ID，用于 demo runner / eval / 文档引用 |
| `title` | 面向人阅读的场景标题 |
| `user_query` | 用户输入文本 |
| `metadata` | 离线模拟输入，如 `image_ids`、`video_ids`、`mock_media`、`demo_data_refs` |
| `expected_tools` | 期望工具调用序列 |
| `expected_response_contains` | 后续 response quality 检查可使用的关键词 |

## 默认场景

当前矩阵覆盖以下 17 个场景：

| scenario_id | 场景 | expected_tools |
| --- | --- | --- |
| `text_chat` | 纯文本聊天 | `[]` |
| `text_image_generation` | 纯文本图片生成 | `image_generation` |
| `image_understanding` | 图片理解 | `vision_understanding` |
| `video_understanding` | 视频理解 | `video_understanding -> render_3d` |
| `product_search_compare` | 文本商品搜索和比价 | `product_search -> price_compare` |
| `image_to_product_search_compare` | 图片找同款并比价 | `vision_understanding -> product_search -> price_compare` |
| `product_search_to_image_generation` | 商品搜索后生成海报 | `product_search -> image_generation` |
| `product_search_to_render` | 商品搜索后进入 3D 渲染 | `product_search -> render_3d` |
| `image_to_render` | 图片进入 3D 渲染 | `vision_understanding -> render_3d` |
| `memory_to_image_generation` | 结合记忆生成图片 | `memory_retrieval -> image_generation` |
| `full_multistep_image_search_compare_generate` | 完整多步图片找同款、比价并生成海报 | `vision_understanding -> product_search -> price_compare -> image_generation` |
| `video_to_product_search` | 视频找商品 | `video_understanding -> product_search` |
| `video_to_render` | 视频进入 3D 渲染 | `video_understanding -> render_3d` |
| `ambiguous_followup` | 歧义输入触发追问 | `[]` |
| `memory_product_to_render` | 商品记忆进入 3D 渲染 | `memory_retrieval -> render_3d` |
| `memory_task_resume` | 任务恢复记忆检索 | `memory_retrieval` |
| `memory_user_isolation` | 用户隔离记忆检索演示 | `memory_retrieval` |

## 离线输入约定

Demo scenario matrix 不提交真实媒体文件。

媒体类场景只使用 metadata 模拟：

```json
{
  "metadata": {
    "input_type": "image",
    "image_ids": ["demo_image_sneaker_1"],
    "mock_media": true
  }
}
```

可引用小规模本地 demo data：

```text
demo_data/products/products.example.json
demo_data/images/.gitkeep
demo_data/videos/.gitkeep
```

## 安全边界

场景矩阵必须满足：

- 默认离线。
- 不依赖真实图片或视频。
- 不包含真实绝对路径。
- 不包含 API Key、Authorization header、Bearer token。
- 不包含完整 base64 图片。
- 不包含真实 Provider raw response。
- 不提交真实生成图片、真实渲染产物或大文件。
- 不触发真实 Provider 调用。

## 后续任务关系

本任务只定义 scenario matrix。

后续任务才会处理：

- Task 069：capability output contract unification。
- Task 070：template-based response composer quality。
- Task 071：eval suite layering。
- Task 072：offline E2E demo runner。

不要在 scenario matrix 中提前实现 runner 或 response composer 行为。
