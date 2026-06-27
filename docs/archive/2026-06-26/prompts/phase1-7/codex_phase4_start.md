请先阅读 AGENTS.md、docs/26-phase4-roadmap.md、tasks/README_PHASE4.md。

当前 Phase 3 已完成，默认 Runtime 已是 AgentGraphRuntime。Phase 4 暂不引入 Harness 概念，先聚焦真实 Provider 可选接入、WebSocket 长任务事件、Memory 检索、失败恢复、Trace、API 协议版本和发布清理。

请从 tasks/029-real-provider-adapter-optional.md 开始。

执行规则：

- 每次只执行一个 task。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要默认调用真实外部 Provider。
- 默认使用 MockAdapter。
- 完成 Task 029 后运行验收测试并停止。
