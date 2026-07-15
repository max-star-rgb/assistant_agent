---
name: assistant-agent-development-testing
description: Use when implementing a feature, fixing a defect, or refactoring behavior in the assistant_agent repository.
---

# Assistant Agent 开发阶段测试

开发必须先形成测试决策，但不无条件新增永久测试或运行完整套件。以 `tests/README.md` 和
`tests/scope-map.toml` 为分层与路由权威。

**新增行为或缺陷修复必须使用：** `superpowers:test-driven-development`。

## 决策

检查邻近测试后，为变更选择一个主要决策：

| 决策 | 使用条件 | 动作 |
| --- | --- | --- |
| ADD | 新稳定契约或具名缺陷没有保护 | 在最窄 scope 先增加一个正确失败的测试。 |
| EXTEND | 权威测试位置正确，但缺少本次边界 | 扩展现有文件/参数，先验证新增 node 正确失败。 |
| REUSE | 纯行为保持重构已被充分保护 | 修改前后运行同一组定向测试，不新增测试。 |
| STAGE | 探索或阶段验收尚未形成稳定契约 | 可暂存测试；阶段结束必须归档或删除。 |
| NO-TEST | 纯文档、机械修改或无有意义自动断言 | 不新增 pytest，执行适当静态或手工验证。 |

不得因“正在开发”同时给多个层级添加相同断言。新增测试必须保护现有测试未覆盖的稳定边界。

## 阶段测试退出

阶段结束时逐项处理 STAGE 测试：稳定领域契约去掉阶段命名后归入唯一 scope；具名历史缺陷可标记
`regression`；只有快速、离线、跨 scope 的安全底座才进入 `critical`。仅证明里程碑完成、探索
假设或重复已有断言的测试必须删除。禁止 phase/stage 命名或“稍后清理”标记进入阶段提交。

## 验证范围

1. 开发循环只运行新增/修改 node 与直接相关回归。
2. 阶段结束运行 critical 与所有受影响 scope；只有窄层无法证明 wiring 时增加一条 scripted/fake
   Provider 的离线跨层验收。
3. 完整 offline suite 仅按 `tests/README.md`“命令”一节的 `--full` 门槛运行；不得把普通局部开发
   或“跨了不止一层”自行升级为 full。
4. 不运行 integration 或真实 Provider，除非用户明确授权对应 opt-in profile。

## 边界与汇报

普通开发发现全仓测试债务时只报告候选，不顺带清理；审计、去重、分层或删除必须由用户显式触发
`$assistant-agent-test-governance`。最终报告说明采用的决策、测试资产的新增/复用/归档/删除情况，
以及实际验证命令；默认不在开发开始时额外展示决策。
