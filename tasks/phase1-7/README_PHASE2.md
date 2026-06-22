# Phase 2 Tasks

本目录中的 014-020 接在 000-013 后执行。目标是从 MVP 骨架进入真实 Agent 编排能力。

执行顺序：

```text
014 LangGraph 最小接入
015 LangGraph 条件路由
016 多步任务规划
017 真实 Provider 接入准备
018 记忆持久化
019 Agent 评估集
020 技术债清理与架构审计
```

执行规则：

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 不自动安装依赖。
- 不调用真实外部服务，除非 task 明确要求并且用户确认。
- 修改源码、测试、文档优先使用 apply_patch。
- 只有 retry without sandbox、联网、安装依赖、仓库外修改等越权行为需要询问用户。
