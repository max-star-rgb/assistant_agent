# 开发阶段测试决策 Skill 设计

## 背景

仓库已完成 scoped 测试迁移，裸 pytest 是快速 critical 底座，完整 offline suite 只在高风险门槛
运行。现有治理 Skill 适合显式全仓清理，但普通开发仍缺少统一回答：是否新增测试、阶段测试何时
退出、何时需要 full。

## 设计

新增自动触发的 `assistant-agent-development-testing`。它在功能实现、缺陷修复和行为重构时，从
ADD、EXTEND、REUSE、STAGE、NO-TEST 中选择一个主要决策。TDD 约束新增行为和缺陷修复；已有
充分契约保护的纯重构复用修改前后的同一组测试。

阶段验收与永久测试分离。临时测试可以帮助探索和验收，但阶段结束必须按稳定领域契约、具名回归
或 critical 安全底座归档，否则删除；phase/stage 命名不得进入阶段提交。

开发循环只运行直接相关节点，阶段结束运行 critical 与受影响 scope。完整 offline suite 仅用于
共享测试基础设施或路由、核心安全底座行为、三个及以上 scope、发布/合并门槛或用户明确要求。

## 边界

Skill 只做当前开发的测试判断，不执行全仓审计。发现测试债务时只报告；去重、分层和删除仍须用户
显式调用 `assistant-agent-test-governance`。Codex 默认只在最终报告中呈现决策和验证证据。

## 验收

Fresh-agent 压力场景应正确处理纯重构、阶段开发、两 scope 普通功能、具名缺陷和纯文档修改；
Skill 结构通过 validator，仓库 critical 契约测试通过，且默认不运行 full 或真实 Provider。

## 行为验证证据

RED 基线显示现有仓库规则已能避免明显的 phase 文件和局部 full，但没有统一输出测试决策，无法
精确区分两 scope 与三 scope，也没有完整描述临时阶段测试从暂存到归档/删除的生命周期。

加载新 Skill 后重放：纯重构选择 REUSE；三阶段开发选择 STAGE、EXTEND/ADD、REUSE，并在每阶段
清零临时测试；tools 与 api 两 scope 不 full，prompt、context、runtime 三 scope 执行 full；纯文档
选择 NO-TEST；未获授权的旧测试重复只报告，不触发治理 Skill。
