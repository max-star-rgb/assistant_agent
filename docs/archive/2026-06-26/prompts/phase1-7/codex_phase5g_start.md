请先阅读 AGENTS.md、docs/84-phase5g-video-understanding-roadmap.md、tasks/README_PHASE5G.md。

当前 Phase 5F Hybrid Intent Router & Planner Quality 已完成。现在进入 Phase 5G：Video Understanding as External MLLM Capability。

Phase 5G 只做轻量 video_understanding capability，不做视频模型工程。

核心原则：

Agent 只负责识别用户是否需要视频理解、校验 video 输入、调用 VideoUnderstandingTool，并将 VideoUnderstandingResult 传给后续能力。
外部 Video MLLM / VLM Provider 负责真正的视频理解。

请从 tasks/081-phase5g-video-understanding-roadmap.md 开始。

执行规则：

- 每次只执行一个 task。
- 只做当前 task 的 Scope，不跨 task 实现。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 不要提交真实视频、视频帧、日志、大文件或真实 Provider 输出样本。
- 不要自研视频模型。
- 不要实现复杂抽帧系统。
- 不要实现实时 WebRTC。
- 不要实现视频数据库或视频监控平台。
- 不要默认调用真实 Video Provider。
- 默认运行路径必须使用 MockVideoUnderstandingAdapter。
- 默认 python -m pytest 必须离线运行，不得调用真实 Video Provider。
- 默认 python scripts/run_evals.py 必须离线运行，不得调用真实 Video Provider。
- 默认 python scripts/run_demo_flows.py 必须离线运行，不得调用真实 Video Provider。
- 真实 Video Provider 只允许在用户手动设置环境变量，并显式运行 smoke 脚本或 RUN_INTEGRATION_TESTS=1 的 integration tests 时触发。
- 如果缺少 API Key、Base URL、模型名或额外依赖，必须返回清晰提示或 skip，不得失败，也不得自动安装依赖。
- 完成 Task 081 后运行 python -m pytest 并停止。
