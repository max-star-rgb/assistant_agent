请先阅读 AGENTS.md、docs/41-phase5a-assistant-capability-routing-roadmap.md、docs/42-assistant-capability-routing-baseline.md、tasks/README_PHASE5A.md。

业务目标更正：

本项目不是 Video-first Agent，也不是 Vision-first Agent，而是 Intent-driven Assistant Agent。

Agent 应根据用户意图自主选择能力，包括：

- direct_chat
- image_generation
- image_understanding
- video_understanding
- product_search
- price_compare
- render_3d
- memory_retrieval
- multi_step_orchestration

其中 direct_chat 和 image_generation 必须支持纯文本输入，不依赖图片或视频。

真实 Qwen Vision smoke 已经跑通，但它只是 image/video understanding capability 的 Provider validation，不是 Phase 5A 主线。

请从 tasks/041-assistant-capability-routing-roadmap.md 开始。

执行规则：

- 每次只执行一个 task。
- 只做当前 task 的 Scope，不跨 task 实现。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 不要提交真实图片、视频、日志、大文件或真实 Provider 输出样本。
- 不要默认调用真实外部 Provider。
- 默认运行路径必须使用 MockAdapter。
- 默认 python -m pytest 必须离线运行，不得调用真实 Provider。
- 默认 python scripts/run_evals.py 必须离线运行，不得调用真实 Provider。
- 真实 Provider 只允许在用户手动设置环境变量，并显式运行 smoke 脚本或 RUN_INTEGRATION_TESTS=1 的 integration tests 时触发。
- 如果缺少 API Key、Base URL、模型名或额外依赖，必须返回清晰提示或 skip，不得失败，也不得自动安装依赖。
- 如果需要验证真实 Provider，请只添加文档、smoke 脚本或 env-gated integration test，不要自动执行真实调用。
- 所有错误信息、trace、日志、测试输出都不得包含 API Key、Authorization header、Bearer token、完整 base64 图片或敏感文件路径。
- 完成 Task 041 后运行 python -m pytest 并停止。
