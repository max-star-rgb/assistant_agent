---
name: assistant-agent-development-testing
description: Use when implementing a feature, fixing a defect, or refactoring behavior in the assistant_agent repository.
---

# Assistant Agent 开发阶段测试

开发必须先形成测试决策，但不无条件新增永久测试。`AGENTS.md` 只提供导航，具体测试规则以
`tests/README.md` 为唯一权威。

## 决策

检查邻近测试后，为变更选择一个主要决策：

| 决策 | 使用条件 | 动作 |
| --- | --- | --- |
| ADD | 新稳定契约或具名缺陷没有保护 | 在最低成本的公开边界增加一个正确失败的测试。 |
| EXTEND | 现有测试文件与本次变更属于同一稳定行为边界 | 扩展现有文件，先验证新增场景正确失败。 |
| REUSE | 纯行为保持重构已被充分保护 | 修改前后运行同一组定向测试，不新增测试。 |
| NO-TEST | 纯文档、机械修改或无有意义自动断言 | 不新增 pytest，执行适当静态或手工验证。 |

不得因“正在开发”同时给多个层级添加相同断言。新增测试必须保护现有测试未覆盖的稳定边界。
不要把 `tests/test_safety_net.py` 当作默认收纳文件；独立契约或故障域应使用聚焦命名的测试文件。

## 验证范围

1. 默认运行唯一的最小离线安全网 `python -m pytest -q`。
2. 只有具名 bug、稳定外部契约或高风险机制需要新增/修改测试。
3. 不运行真实 Provider，除非用户明确授权对应 opt-in profile。

## 边界与汇报

普通开发发现测试债务时只报告候选，不顺带清理。最终报告按 `tests/README.md` 的固定 `Tests:` 句式
说明决策，并列出实际验证命令。
