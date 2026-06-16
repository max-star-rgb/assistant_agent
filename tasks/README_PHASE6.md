# Phase 6 Tasks：Productization / Usable Demo

Phase 6 从 Task 108 开始。该阶段不新增核心 capability，而是把 Agent 变成可运行、可演示、可配置的小产品。

## 阶段划分

```text
Phase 6A Local Demo Entry / CLI
Phase 6B FastAPI Demo & Simple Web Console
Phase 6C Real Provider Opt-in Demo
Phase 6D Local Deployment / Config / Observability
Phase 6E Documentation Consolidation / Release Review
```

## Task 顺序

```text
108 Phase 6 Productization Overall Roadmap

6A:
109 Assistant CLI / Local Demo Entry
110 Demo Scenario Polish
111 Phase 6A Review

6B:
112 FastAPI Demo Contract Stabilization
113 Simple Web Console
114 Phase 6B Review

6C:
115 Real Provider Opt-in Runbooks
116 Real Provider Smoke Matrix
117 Phase 6C Review

6D:
118 Local Deployment and Configuration
119 Healthcheck / Trace / Observability
120 Phase 6D Review

6E:
121 Documentation Consolidation
122 Release Checklist and Cleanup
123 Phase 6 Review
```

## 执行规则

- 建议按 6A → 6B → 6C → 6D → 6E 串行执行。
- 每个阶段可以用对应 skill 自动执行。
- 默认 mock/local。
- 默认不调用真实 Provider。
- 不写 API Key。
- 不提交真实用户数据、真实媒体、生成图或渲染产物。
- 不做生产权限系统。
- 不做 Kubernetes。
- 修改源码、测试、文档优先使用 apply_patch。
