请先阅读 AGENTS.md、docs/48-phase5b-text-first-capabilities-roadmap.md、tasks/README_PHASE5B.md。

当前 Phase 5A Assistant Capability Routing Baseline 已完成。现在进入 Phase 5B：Text-first Capabilities。

Phase 5B 只聚焦两个能力：

- direct_chat
- image_generation

这两个能力必须支持纯文本输入，不依赖图片或视频。

不要接入商品搜索、比价、3D 渲染或新的 Vision hardening。不要进入完整 Phase 5。

请从 tasks/048-phase5b-text-first-capabilities-roadmap.md 开始。

执行规则：

- 每次只执行一个 task。
- 只做当前 task 的 Scope，不跨 task 实现。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 不要提交真实生成图片、日志、大文件或真实 Provider 输出样本。
- 不要默认调用真实外部 Provider。
- 默认运行路径必须使用 MockAdapter。
- 默认 python -m pytest 必须离线运行，不得调用真实 Provider。
- 默认 python scripts/run_evals.py 必须离线运行，不得调用真实 Provider。
- 真实 Provider 只允许在用户手动设置环境变量，并显式运行 smoke 脚本或 RUN_INTEGRATION_TESTS=1 的 integration tests 时触发。
- 如果缺少 API Key、Base URL、模型名或额外依赖，必须返回清晰提示或 skip，不得失败，也不得自动安装依赖。
- 完成 Task 048 后运行 python -m pytest 并停止。
