---
name: assistant-agent-development-testing
description: Use when adding or updating pytest tests, deciding the verification scope for a code change, or diagnosing deterministic pytest failures in the assistant_agent repository.
---

# Assistant Agent Pytest 测试指导

本 skill 只提供 pytest 测试设计、验证范围选择和失败诊断的 workflow 入口，不指导功能实现，也不保存
测试规则。开始测试工作前必须完整读取
`tests/README.md`，并以其中的测试分层、目录归属、测试决策、验证范围和汇报格式为唯一权威。

## Workflow

1. 读取 `tests/README.md`；
2. 搜索邻近测试并按该文档形成测试决策；
3. 执行能够证明本次变更的最小定向测试；只有出现失败证据、无法证明 wiring 或命中全量测试条件时，
   才扩大验证范围；
4. 不得把裸 `pytest -q` 当作每个开发任务的默认安全网；
5. 按该文档规定的格式汇报测试决策、实际命令以及未执行全量测试的正常决策。

若 workflow 与 `tests/README.md` 不一致，始终以 `tests/README.md` 为准并修正本入口，不在 skill 中
复制具体规则。
