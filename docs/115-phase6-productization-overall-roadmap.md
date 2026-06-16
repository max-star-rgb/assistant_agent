# 115 Phase 6 总路线图：Productization / Usable Demo

## 背景

Phase 5A 到 Phase 5J 已经完成了 Assistant Agent 的核心能力体系：

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
provider_safety
memory_hardening
mcp_skills_packaging
```

Phase 6 不继续堆 Agent 抽象，也不继续扩展新 capability。Phase 6 的目标是把项目从“工程能力完整”推进到“用户可以实际运行、演示、调试、配置”的可用产品 Demo。

## Phase 6 总目标

让用户能通过 CLI、API、简单 Web 控制台或 runbook 完成端到端任务：

```text
输入自然语言
  ↓
Agent 识别意图
  ↓
调用已有 capability
  ↓
展示自然回复
  ↓
展示 tool calls / trace / errors
  ↓
可选启用真实 Provider
```

## Phase 6 分阶段

```text
Phase 6A：Local Demo Entry / CLI
Phase 6B：FastAPI Demo & Simple Web Console
Phase 6C：Real Provider Opt-in Demo
Phase 6D：Local Deployment / Config / Observability
Phase 6E：Documentation Consolidation / Release Review
```

## 执行策略

Phase 6 文档可以一次性给出，但执行必须分阶段进行：

```text
6A → 6B → 6C → 6D → 6E
```

不要直接用一个 Codex 进程一次性从 6A 跑到 6E，除非你已经完成一次人工检查。每个阶段完成后应审计，再决定是否进入下一阶段。

## 不做什么

Phase 6 暂不做：

- 新增核心 capability。
- 默认调用真实 Provider。
- 真实外部 API 大规模接入。
- 生产级多用户权限系统。
- Kubernetes 部署。
- 复杂前端产品。
- 计费系统。
- 真实支付、下单、购买。
- 公开部署 MCP 服务。

## 默认安全边界

- 默认 mock/local。
- 默认 pytest 离线。
- 默认 eval 离线。
- 默认 demo runner 离线。
- 真实 Provider 只能 opt-in。
- API Key 只放用户本地环境变量。
- 不提交真实用户数据。
- 不提交真实媒体。
- 不提交真实生成图、渲染产物或 Provider raw response。

## Phase 6 完成标准

Phase 6 结束时应满足：

```text
CLI 可用
FastAPI demo 可用
简单 Web console 或 demo page 可用
真实 Provider opt-in runbook 可用
Docker/local deployment 可用
Trace/debug 查询可用
README/Quickstart 清晰
发布检查通过
```
