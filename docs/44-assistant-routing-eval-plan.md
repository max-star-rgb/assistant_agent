# 44 Assistant Routing Eval Plan

## 目标

建立覆盖所有 capability 的离线路由评估集。

默认 eval 不调用真实 Provider，只检查：

- intent 是否正确。
- capability 是否正确。
- 工具调用顺序是否正确。
- 是否错误要求媒体。
- 是否错误触发视觉理解。
- 是否正确处理多步任务。

## Eval 分类

```text
direct_chat
text_only_image_generation
text_only_product_search
text_only_price_compare
text_only_render
image_understanding
video_understanding
media_plus_generation
media_plus_search_compare
memory_retrieval
multi_step_orchestration
ambiguous_followup
```

## Case 示例

### Direct Chat

```json
{
  "id": "direct_chat_001",
  "user_query": "帮我写一段商品介绍",
  "inputs": {"has_image": false, "has_video": false},
  "expected_intent": "direct_chat",
  "expected_tools": [],
  "must_not_call": ["image_understanding", "video_understanding"]
}
```

### Text-only Image Generation

```json
{
  "id": "text_image_generation_001",
  "user_query": "生成一张赛博朋克风格海报",
  "inputs": {"has_image": false, "has_video": false},
  "expected_intent": "image_generation",
  "expected_tools": ["image_generation"],
  "must_not_require": ["image", "video"]
}
```

### Media + Generation

```json
{
  "id": "media_generation_001",
  "user_query": "用这张图生成一张电商海报",
  "inputs": {"has_image": true, "has_video": false},
  "expected_intent": "multi_step_orchestration",
  "expected_tools": ["image_understanding", "image_generation"]
}
```

### Multi-step

```json
{
  "id": "multistep_001",
  "user_query": "帮我找这张图里的鞋子，比较价格，再生成海报",
  "inputs": {"has_image": true, "has_video": false},
  "expected_intent": "multi_step_orchestration",
  "expected_tools": ["image_understanding", "product_search", "price_compare", "image_generation"]
}
```

## 指标

```text
intent_accuracy
capability_accuracy
tool_selection_accuracy
ordered_tool_match
unexpected_tool_rate
media_requirement_error_rate
followup_accuracy
```

## 验收标准

- 至少覆盖 40 条 routing cases。
- 默认离线运行。
- 不调用真实 Provider。
- 能输出 failed_case_ids。
