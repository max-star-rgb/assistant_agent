# 26 Phase 4 路线图

## 背景

Phase 3 后，项目已经从 MVP 骨架升级为以 LangGraph 为默认 Runtime 的多模态 Agent 系统：

- 默认 Runtime 已切换为 `AgentGraphRuntime`。
- HTTP API 和 WebSocket 已默认通过 Graph Runtime 执行。
- `AgentWorkflow.run()` 已兼容委派到 Graph Runtime。
- Graph Node 边界已拆分为独立组件。
- Memory Backend 支持 `memory` 与 `jsonl`。
- Provider Contract Tests 已建立。
- Eval Cases 已扩展到 30 条。
- 多步任务由 LangGraph loop 驱动。

Phase 4 暂不引入 Harness 概念，先聚焦生产化边界。

## Phase 4 总目标

Phase 4 的目标是把当前本地 Mock/离线 Agent Runtime 推进到“可接真实能力、可观测、可恢复、API 稳定”的工程状态。

核心目标：

1. 增加真实 Provider Adapter 的可选实现。
2. 保持默认测试离线，不调用真实 Provider。
3. 将 WebSocket 从同步事件升级为长任务事件流。
4. 引入本地任务队列抽象，先不强依赖 Redis/Celery。
5. 增强 Memory 检索策略：关键词、类型过滤、摘要压缩。
6. 增加失败恢复策略：跳过、重试、降级、请求确认。
7. 增加 Graph Execution Trace。
8. 定义 API 错误码与响应协议版本。
9. 清理缓存、构建产物和发布边界。

## Phase 4 执行顺序

```text
029 真实 Provider Adapter 可选实现
030 Provider Integration Tests 完善
031 长任务事件流与 WebSocket 升级
032 本地任务队列抽象
033 Memory 检索策略增强
034 失败恢复策略
035 Graph Execution Trace
036 API 错误码与响应协议版本
037 发布清理与 Phase 4 架构审计
```

## 不做什么

Phase 4 暂不做：

- Harness 架构重命名。
- 大规模目录搬迁。
- 强依赖云服务。
- 默认调用付费 API。
- 直接上生产级分布式任务队列。
- 默认接 Vector DB。
- 引入复杂权限系统。

## 执行规则

- 每次只执行一个 task。
- 默认使用 MockAdapter。
- 真实 Provider 必须通过环境变量显式启用。
- Integration tests 默认 skip。
- 不自动联网安装依赖。
- 不自动写入 API Key。
- 修改源码、测试、文档优先使用 `apply_patch`。
- 完成一个 task 后运行测试并停止。
