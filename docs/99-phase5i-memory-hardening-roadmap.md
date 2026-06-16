# 99 Phase 5I 路线图：Memory Hardening

## 背景

前序阶段已经完成了 Assistant Agent 的主要能力基线：

```text
direct_chat
image_generation
image_understanding
video_understanding
product_search
price_compare
render_3d
memory_retrieval
multi_step_orchestration
```

Phase 5H 已经补强 Provider Safety / Retry / Cost / Trace Query。接下来进入 Phase 5I：

```text
Memory Hardening
```

当前 memory 能力已经存在，但仍偏 mock/local baseline。Phase 5I 的目标不是做复杂知识库平台，也不是直接接入向量数据库，而是让助理 Agent 的记忆能力更稳定、更安全、更可控、更适合多轮任务。

## Phase 5I 总目标

让 Agent 能更可靠地处理：

```text
上次那个商品
之前那张图
我喜欢的风格
刚才生成的海报
之前比较过的商品
把上次那个包放到客厅里渲染
按我喜欢的日系极简风格生成图片
```

核心目标：

1. 统一 memory 数据模型。
2. 明确 memory 类型和写入策略。
3. 改进 memory 检索排序和上下文构造。
4. 支持 session memory / user preference memory / task memory / artifact memory。
5. 增加隐私、安全、用户隔离和过期策略。
6. 让 memory 结果稳定参与 planner / response composer。
7. 增加 memory eval、API、demo 覆盖。
8. 默认 local-first，不接真实外部记忆服务。
9. 不默认接 Vector DB，不做复杂 RAG 平台。

## Memory 类型

建议至少支持：

```text
conversation_memory
preference_memory
product_memory
artifact_memory
task_memory
video_memory
image_memory
render_memory
```

### conversation_memory

保存对话摘要和多轮上下文。

### preference_memory

保存用户偏好，例如：

```text
喜欢日系极简
偏好浅色背景
预算 500 元以内
偏好白色低帮鞋
```

### product_memory

保存用户看过、搜索过、比较过的商品。

### artifact_memory

保存生成图、渲染图、结果引用。

### task_memory

保存已完成或部分完成的任务状态。

### video_memory / image_memory

保存图片/视频理解摘要，而不是原始媒体文件。

## Phase 5I 不做什么

本阶段不做：

- 生产级向量数据库。
- 复杂 RAG 平台。
- 用户画像商业系统。
- 敏感个人信息采集。
- 跨用户共享记忆。
- 默认上传记忆到外部服务。
- 默认接 PostgreSQL / Qdrant / Milvus。
- 自动保存所有原文和媒体。
- 长期保存敏感数据。
- MCP / Skills 打包。

## 推荐存储策略

默认保持 local-first：

```text
InMemoryStore
JsonlMemoryStore
```

可以预留：

```text
SqliteMemoryStore
VectorMemoryStore skeleton
```

但 Phase 5I 不强制实现真实向量数据库。

## Phase 5I 任务顺序

```text
094 Phase 5I Memory Hardening Roadmap
095 Memory Data Model and Store Boundary
096 Memory Retrieval Ranking and Context Builder
097 Memory Write Policy and Lifecycle
098 Memory Privacy and User Isolation
099 Memory Eval / API / Demo Coverage
100 Phase 5I Review
```

## 默认安全边界

- 默认使用 local memory store。
- 默认测试不调用外部 memory service。
- 不上传 memory 到外部服务。
- 不提交真实用户记忆。
- 不保存 API Key、Authorization、Bearer token。
- 不保存完整 base64、原始媒体、大文件内容。
- 不跨 user_id 检索记忆。
- 记忆写入必须可控，不自动保存所有内容。
- 记忆上下文必须有长度限制和脱敏。

## Phase 5I 完成后

Phase 5I 完成后，可考虑：

```text
Phase 5J MCP / Skills Packaging
Phase 6 Productization / UI / Deployment
```

如果仍有真实 Provider 稳定性问题，也可先补 Provider hardening 小任务，而不是直接做 MCP。
