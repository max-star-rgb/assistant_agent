请先阅读 AGENTS.md、docs/55-phase5c-product-search-price-compare-roadmap.md、tasks/README_PHASE5C.md。

当前 Phase 5A Assistant Capability Routing Baseline 已完成，Phase 5B Text-first Capabilities 已完成。现在进入 Phase 5C：Product Search / Price Compare Provider Baseline。

Phase 5C 只聚焦两个能力：

- product_search
- price_compare

不要升级意图识别，不要接入 3D render，不要做 Vision hardening，不要进入 Harness Engineering。

请从 tasks/055-phase5c-product-search-price-compare-roadmap.md 开始。

执行规则：

- 每次只执行一个 task。
- 只做当前 task 的 Scope，不跨 task 实现。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 不要提交大规模真实商品数据、日志、大文件或真实 Provider 输出样本。
- 不要写爬虫。
- 不要处理登录、cookie、购买、下单、支付。
- 不要默认调用真实外部商品/价格 Provider。
- 默认运行路径必须使用 MockAdapter 或 LocalJsonAdapter。
- 默认 python -m pytest 必须离线运行，不得联网。
- 默认 python scripts/run_evals.py 必须离线运行，不得调用真实 Provider。
- 真实 Provider 只允许在用户手动设置环境变量，并显式运行 smoke 脚本或 RUN_INTEGRATION_TESTS=1 的 integration tests 时触发。
- 如果缺少 API Key、Base URL、模型名或额外依赖，必须返回清晰提示或 skip，不得失败，也不得自动安装依赖。
- 完成 Task 055 后运行 python -m pytest 并停止。
