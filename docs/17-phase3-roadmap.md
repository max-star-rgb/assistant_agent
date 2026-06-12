# 17 Phase 3 路线图

## 背景

Phase 2 审计结论显示，当前项目已经具备一个可本地测试的多模态 Agent MVP：Schema、AgentState、Tool、Adapter、Memory、FastAPI、WebSocket、E2E、Eval 都已经存在；LangGraph 已经通过 graph.py 和 conditional_graph.py 接入。

但仍存在关键差距：默认 API 和 AgentWorkflow.run() 仍主要走自研同步 workflow；LangGraph 还是旁路实现；多步规划存在，但还没有完全由 LangGraph 循环节点驱动；JSONL MemoryStore 已存在，但尚未成为主 workflow 的长期记忆策略；Provider 测试体系也还不完整。

## Phase 3 总目标

Phase 3 的目标不是继续堆功能，而是把已有 MVP 升级为更接近生产形态的 Agent Runtime。

核心目标：

1. LangGraph 成为默认编排入口。
2. Graph 节点不再依赖 workflow 私有方法。
3. MemoryStore 可配置，并接入主 Agent 流程。
4. Provider Adapter 有统一契约测试。
5. Eval 集覆盖更真实的中文指令、多步任务和失败场景。
6. 多步计划由 LangGraph 显式循环执行。
7. API、WebSocket、E2E 都走新的 graph runtime。
8. 仓库产物、缓存、测试边界清理干净。

## Phase 3 执行顺序

```text
021 将 LangGraph 设为默认 Agent Runtime
022 拆分 Graph Node 与 Workflow 私有方法边界
023 接入可配置持久化 MemoryStore
024 增加 Provider Adapter 契约测试
025 扩展 Agent Eval 集
026 LangGraph 多步循环执行
027 API/WebSocket 使用 Graph Runtime
028 仓库清理与 Phase 3 发布检查
```

## 执行原则

- 每次只执行一个 task。
- 每个 task 必须有测试。
- 不自动联网安装依赖。
- 不自动接入真实外部服务。
- 默认仍使用 MockAdapter。
- 真实 Provider 测试必须通过环境变量显式开启。
- 修改源码、测试、文档优先使用 apply_patch。
- 不要用 python -c write_text 作为常规改文件方式。
- 完成一个 task 后运行测试并停止。
