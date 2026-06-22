# Phase 3 Tasks

Phase 3 接在 014-020 后执行。目标是让项目从 MVP 骨架升级为可维护的 LangGraph Agent Runtime。

## 执行顺序

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

## 执行规则

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 不自动安装依赖。
- 不调用真实外部服务，除非 task 明确要求并且用户确认。
- 默认使用 MockAdapter。
- 修改源码、测试、文档优先使用 apply_patch。
- 只有 retry without sandbox、联网、安装依赖、仓库外修改等越权行为需要询问用户。

## 完成标准

每个 task 完成后：

```bash
python -m pytest
```

如果 task 涉及 eval：

```bash
python scripts/run_evals.py
```

完成后停止，等待用户确认。
