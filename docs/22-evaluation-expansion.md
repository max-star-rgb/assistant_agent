# 22 Agent Eval 扩展设计

## 目标

把固定评估集扩展为更贴近真实中文用户的 Agent 评估体系。

## 当前状态

已有：

```text
tests/evals/eval_cases.json
scripts/run_evals.py
```

但样例仍偏规则化，需要扩展中文口语表达、模糊指代、多步依赖、失败恢复、多模态输入组合、记忆检索。

## 推荐分类

```text
intent/
routing/
multistep/
memory/
failure/
multimodal/
```

## 推荐 case 字段

```json
{
  "id": "multistep_search_compare_generate_001",
  "user_query": "帮我找下视频里那双鞋，看看哪家便宜，再做张海报",
  "inputs": {
    "has_video": true,
    "has_image": false
  },
  "expected_intent": "multi_tool",
  "expected_tools": [
    "vision_understanding",
    "product_search",
    "price_compare",
    "image_generation"
  ],
  "must_not_call": [
    "render_3d"
  ]
}
```

## 指标

```text
intent_accuracy
tool_selection_accuracy
ordered_tool_match
task_completion_rate
unexpected_tool_rate
memory_recall_rate
```

## 验收标准

- Eval cases 至少达到 30 条。
- run_evals 输出各类指标。
- 支持失败 case 展示。
- 不依赖真实 Provider。
