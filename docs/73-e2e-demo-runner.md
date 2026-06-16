# 73 E2E Demo Runner

## 目标

新增一个默认离线的 demo runner，用于快速演示 Assistant Agent 的完整能力链路。

## 推荐脚本

```text
scripts/run_demo_flows.py
```

## 默认行为

默认使用：

```text
MockAdapter
LocalJsonAdapter
InMemory/Jsonl local store
```

默认不使用：

```text
真实 Provider
真实 API Key
真实商品 API
真实渲染服务
真实图片生成服务
```

## CLI 示例

运行全部 demo：

```bash
python scripts/run_demo_flows.py
```

运行指定 demo：

```bash
python scripts/run_demo_flows.py --scenario text_chat
python scripts/run_demo_flows.py --scenario product_search_compare
python scripts/run_demo_flows.py --scenario image_to_search_compare_generate
```

输出格式：

```json
{
  "scenario_id": "product_search_compare",
  "status": "succeeded",
  "tool_sequence": ["product_search", "price_compare"],
  "response_text": "...",
  "errors": [],
  "run_id": "...",
  "trace_id": "..."
}
```

## Demo Scenario 文件

建议新增：

```text
demo_data/scenarios/e2e_demo_scenarios.json
```

字段：

```text
scenario_id
title
user_query
metadata
expected_tools
expected_response_contains
```

## 推荐场景

```text
text_chat
text_image_generation
image_understanding
video_understanding
product_search_compare
image_to_product_search_compare
product_search_to_image_generation
product_search_to_render
image_to_render
memory_to_image_generation
full_multistep_image_search_compare_generate
```

## 输出要求

- 输出 JSON summary。
- 可选输出 Markdown summary。
- 不输出 API Key。
- 不输出 provider raw response。
- 不输出完整 base64。
- 不要求真实媒体文件。
- 媒体场景可通过 metadata 模拟，或使用 `.gitkeep` 占位。

## 验收标准

- runner 默认可离线运行。
- 至少 6 个 demo scenario 可跑通。
- 每个 scenario 输出 tool_sequence。
- response_text 不是通用“已完成请求处理”。
- 可用于 README 演示。
