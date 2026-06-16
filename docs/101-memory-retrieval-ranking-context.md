# 101 Memory Retrieval Ranking and Context Builder

## 目标

改进 memory 检索质量，让 Agent 能稳定处理“上次那个、之前喜欢的风格、刚才生成的图”等历史指代。

## 当前问题

简单关键词检索可能会出现：

```text
命中错误记忆
返回过多无关内容
跨 session 混乱
上下文太长
用户偏好和任务记忆混在一起
```

Phase 5I 要先做 local-first 的轻量检索策略，不直接引入向量数据库。

## 检索信号

建议综合：

```text
query keyword match
memory_type match
tag match
session_id match
recency
artifact_ref availability
user preference priority
task relevance
```

## Ranking Strategy

建议实现：

```text
score = keyword_score + type_score + tag_score + recency_score + session_score
```

不需要复杂 ML 模型。

## Memory Context Builder

检索结果不应原样塞进 prompt。应构造简短上下文：

```text
相关历史：
1. 用户偏好日系极简、浅色背景。
2. 上次比较过一款白色低帮运动鞋，最低价 329 元。
3. 最近生成过一张商品海报，引用为 mock://image/generated/poster.png。
```

## Context 限制

建议配置：

```text
MULTIMODAL_AGENT_MEMORY_CONTEXT_MAX_ITEMS=5
MULTIMODAL_AGENT_MEMORY_CONTEXT_MAX_CHARS=1200
```

## 类型优先级

不同任务可有不同优先级。

### image_generation

优先：

```text
preference
artifact
product
conversation
```

### product_search

优先：

```text
preference
product
conversation
```

### render_3d

优先：

```text
preference
product
artifact
render
```

### direct_chat

优先：

```text
conversation
preference
```

## 引用解析

“上次那个”应优先匹配：

```text
recent task memory
recent product memory
recent artifact memory
```

“我喜欢的风格”应优先匹配：

```text
preference memory
```

## 验收标准

- memory search 支持 top_k。
- 支持 memory_type 过滤。
- 支持 session_id 过滤。
- 支持 recency 排序。
- context builder 有字符限制。
- planner / prompt_builder 可使用 memory_context。
- 默认本地离线。
