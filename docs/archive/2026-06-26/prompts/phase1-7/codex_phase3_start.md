请先阅读 AGENTS.md、docs/17-phase3-roadmap.md、tasks/README_PHASE3.md。

当前 Phase 2 已完成，审计结论是：LangGraph 已接入但仍是旁路实现，默认 API 和 AgentWorkflow.run() 还没有完全转向 Graph Runtime。

请从 tasks/021-langgraph-primary-runtime.md 开始。

执行规则：

- 每次只执行一个 task。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要联网安装依赖。
- 不要调用真实外部 Provider。
- 默认使用 MockAdapter。
- 完成 Task 021 后运行验收测试并停止。
