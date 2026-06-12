# 15 Agent 评估设计

## 目标

Phase 2 后，Agent 不只要“能跑”，还要能评估质量。

## 核心指标

```text
Intent Accuracy
Tool Selection Accuracy
Task Completion Rate
Multi-Step Success Rate
Memory Recall Rate
Provider Failure Recovery
Latency
```

## 推荐目录

```text
tests/evals/
├── intent_cases.yaml
├── tool_routing_cases.yaml
├── multistep_cases.yaml
└── memory_cases.yaml
```

## Case 示例

```yaml
- id: search_and_generate_001
  user_query: "找一下视频里的鞋子，再生成一张海报"
  inputs:
    has_video: true
  expected_tools:
    - vision_understanding
    - product_search
    - image_generation
```

## 评估 Runner

新增：

```text
scripts/run_evals.py
```

功能：

- 读取 yaml case。
- 调用 AgentWorkflow 或 LangGraph。
- 检查 expected_tools 是否命中。
- 输出通过率。

## 验收标准

- 至少 10 条 eval case。
- 可以本地离线运行。
- 不依赖真实 Provider。
- 输出 summary。
