---
name: assistant-agent-development-testing
description: Use when implementing a feature, fixing a defect, or refactoring behavior in the assistant_agent repository.
---

# Assistant Agent 开发阶段测试

本 skill 只提供开发任务的测试 workflow 入口，不保存测试规则。开始实现前必须完整读取
`tests/README.md`，并以其中的测试分层、目录归属、测试决策、验证范围和汇报格式为唯一权威。

## Workflow

1. 读取 `tests/README.md`；
2. 搜索邻近测试并按该文档形成测试决策；
3. 实现前后执行该文档要求的定向验证和默认安全网；
4. 按该文档规定的格式汇报测试决策和实际命令。

若 workflow 与 `tests/README.md` 不一致，始终以 `tests/README.md` 为准并修正本入口，不在 skill 中
复制具体规则。
