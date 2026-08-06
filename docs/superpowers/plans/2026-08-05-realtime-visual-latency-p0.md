# 后台视觉延迟 P0 实施计划

**目标：** 让后台视觉延迟可以按本地处理、Provider 交互和语义发布阶段准确归因，并保证 `semantic_publish_latency_ms` 结束于 semantic store 真正可读之后。

## 任务 1：锁定可观测行为

- 新增临时 TDD 测试，验证发布时间戳不早于 semantic store 成功写入。
- 验证内部诊断字段完整投影到 `RealtimeVideoContext`。
- 验证 Qwen fake WebSocket 成功路径输出分阶段耗时。

## 任务 2：修正计时与诊断

- 在文本 embedding 和 semantic store 写入周围分别计时。
- semantic store 写入完成后再生成发布诊断和 rolling memory 快照。
- Qwen adapter 记录 JPEG、建连、session update、media commit、响应首包/尾段、解析阶段耗时。

## 任务 3：贯通观测链路

- 将新增字段投影到 runtime context、`context.build.finished`、live view trace summary 和 turn latency summary。
- 字段保持 prompt-safe，不记录图片、Provider 原文或敏感配置。

## 任务 4：验证与文档

- 运行新 TDD 测试和受影响的既有测试。
- 运行 Ruff 与 diff 检查。
- 更新实时媒体与 observability 权威文档。
