请先阅读 AGENTS.md、docs/36-phase4-5-real-provider-smoke.md、docs/37-api-key-and-env-safety.md、docs/38-demo-data-and-smoke-flow.md、tasks/README_PHASE4_5.md。

当前 Phase 4 已完成。现在不要开始完整 Phase 5，先做 Phase 4.5：真实 Vision Provider Smoke Test 准备。

请从 tasks/038-real-vision-provider-smoke.md 开始。

执行规则：

- 每次只执行一个 task。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 可以更新 .env.example，但只能写变量名和占位说明。
- 不要默认调用真实外部 Provider。
- 默认使用 MockAdapter。
- smoke 脚本只有用户显式运行时才允许尝试真实 Provider。
- 完成 Task 038 后运行 python -m pytest 并停止。
