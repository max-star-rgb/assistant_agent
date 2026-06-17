请使用 skills/phase7-runner/SKILL.md。

请从当前未完成的 Phase 7 task 开始，自动顺序执行到 Task 148 Phase 7G Production Readiness Review 结束。

如果某个 task 已经完成，请跳过并继续下一个未完成 task。

执行规则：

- 只做到 Task 148，不要开始 Phase 8。
- 每个 task 只做自己的 Scope，不跨 task 扩展。
- 修改源码、测试、文档优先使用 apply_patch。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 不要提交真实用户数据、真实媒体、生成图、渲染产物、日志、大文件或 provider raw response。
- 不要默认调用真实 Provider。
- 不要部署到公网。
- 不要实现 Kubernetes。
- 默认 runtime profile 必须是 local_demo。
- 默认 pytest/eval/demo 必须离线。
- 真实 Provider 只允许在 provider_smoke 或 pilot profile 下显式配置，且本阶段不要自动执行真实 smoke。
- Task 148 完成后停止，并给出执行摘要、测试结果、剩余问题和 pilot readiness 建议。
