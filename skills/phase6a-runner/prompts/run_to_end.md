请使用 skills/phase6a-runner/SKILL.md。

请从当前未完成的 Phase 6A Local Demo Entry / CLI task 开始，自动顺序执行到 Task 111 结束。

任务顺序：

- 109 Assistant CLI / Local Demo Entry
- 110 Demo Scenario Polish
- 111 Phase 6A Review

如果某个 task 已经完成，请跳过并继续下一个未完成 task。

本次临时允许连续执行多个 task，不需要每个 task 后等待我确认，但必须完成一个 task 后再进入下一个 task，并运行该 task 的验收命令。

安全规则：

- 只做到 Task 111，不要开始下一阶段。
- 每个 task 只做自己的 Scope，不跨 task 扩展。
- 修改源码、测试、文档优先使用 apply_patch。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要调用真实外部 Provider。
- 默认运行路径必须 mock/local/offline。
- Do not build a complex frontend.
- Do not call real Providers.


Task 111 完成后停止，并给出执行摘要、测试结果、剩余问题和下一阶段建议。
