请先阅读 AGENTS.md、docs/11-langgraph-integration.md、tasks/README_PHASE2.md。

当前 000-013 已完成，但项目尚未真正接入 LangGraph。请从 tasks/014-langgraph-minimal-integration.md 开始。

规则：

- 每次只执行一个 task。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 如果缺少 langgraph，停止并告诉我需要安装什么，不要自动安装。
- 完成 Task 014 后运行验收测试并停止。
