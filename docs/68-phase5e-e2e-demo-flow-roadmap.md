# 68 Phase 5E 路线图：End-to-End Demo Flow & Response Quality

## 背景

Phase 5A 已完成 Assistant Capability Routing Baseline。

Phase 5B 已完成：

```text
direct_chat
image_generation
```

Phase 5C 已完成：

```text
product_search
price_compare
```

Phase 5D 已完成：

```text
render_3d
```

当前 Assistant Agent 已具备核心 capability baseline：

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

Phase 5E 不继续扩展 Provider，不接入真实 Blender / Unity / Three.js，不升级复杂意图识别，不做 Harness Engineering。

Phase 5E 的重点是：

```text
把已有能力串成可演示、可评估、可复现的完整用户场景。
```

Phase 5E 不是新能力扩展阶段，而是 demo flow 与回答质量收敛阶段。所有实现必须继续基于 Phase 5A-5D 已有能力。

## Phase 5E 总目标

Phase 5E 目标是让系统从“能力都能单独调用”升级为“助理 Agent 可以完成端到端用户任务”。

核心目标：

1. 定义完整 demo 场景矩阵。
2. 统一 capability 输出 contract。
3. 改进 response composer，让多步任务返回可读结果，而不是只说“已完成请求处理”。
4. 分层整理 eval suite。
5. 增加 E2E demo runner。
6. 保持默认 mock/local-first。
7. 默认不调用真实 Provider。
8. 为后续 Hybrid Intent Router / Provider Hardening / MCP / Skills 打基础。

## 不做什么

Phase 5E 暂不做：

- 新增真实 Provider。
- 接入新的真实外部 API。
- 真实电商平台接入。
- 真实渲染服务接入。
- 真实图片生成服务接入。
- 复杂 LLM intent router。
- 生产级队列。
- 权限系统。
- Harness Engineering。
- MCP Server。
- Skills 打包。
- MCP / Skills 相关打包、发布或运行时集成。

## 推荐 Demo 场景

至少覆盖：

```text
1. 纯文本聊天
2. 纯文本图片生成
3. 图片理解
4. 视频理解
5. 文本商品搜索 + 比价
6. 图片找同款 + 比价
7. 商品搜索后生成海报
8. 商品 / 图片进入 3D 渲染
9. 结合记忆的连续任务
10. 完整多步任务：图片/视频理解 → 商品搜索 → 比价 → 图片生成或 3D 渲染
```

## Phase 5E 执行顺序

```text
067 Phase 5E E2E Demo Flow Roadmap
068 Demo Scenario Matrix
069 Capability Output Contract Unification
070 Response Composer Quality
071 Eval Suite Layering
072 E2E Demo Runner
073 Phase 5E Review
```

每个 task 只允许完成当前任务 Scope。不要在 roadmap、scenario matrix 或 review 任务中提前实现 runner、contract unification 或 response composer 行为。

## 默认安全边界

- 默认使用 MockAdapter / LocalJsonAdapter。
- 默认 pytest 不调用真实 Provider。
- 默认 eval 不调用真实 Provider。
- Demo runner 默认离线。
- 不写入 API Key。
- 不提交真实图片、视频、生成图片、渲染产物或大规模商品数据。
- 不输出 Authorization header、Bearer token、完整 base64 图片或真实 Provider raw response。

## Phase 5E 完成后

Phase 5E 完成后再决定是否进入：

```text
Phase 5F Hybrid Intent Router / Planner Quality
Phase 5G Provider Safety / Retry / Cost / Trace Query
Phase 5H Memory Hardening
Phase 5I MCP / Skills Packaging
```

不要在 Phase 5E 中提前执行这些后续阶段。
