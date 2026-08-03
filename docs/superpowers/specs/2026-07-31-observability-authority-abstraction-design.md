# Observability 权威文档抽象设计

## 背景

`docs/observability-harness.md` 同时承载稳定架构契约、具体实现字段、开发工具完整用法、
Langfuse 展示细节、真实运行诊断流程和历史阶段计划，已膨胀到难以维护和检索的程度。
本次只重构文档信息架构，不改变运行时、trace schema、日志行为或诊断工具。

## 目标

- 让 `docs/observability-harness.md` 重新成为稳定、抽象、可独立理解的当前架构权威。
- 建立一份现役真实运行诊断 runbook，使拿到真实 trace、通话或机器日志的开发者可以按统一流程取证。
- 删除当前权威中的历史开发过程和重复实现说明，避免源码变更后产生大面积文档漂移。
- 同步仓库入口，使架构问题和实际诊断问题分别路由到正确文档。

## 非目标

- 不修改任何 Python 源码、schema、事件名称、日志格式或 CLI 行为。
- 不重写其他专项架构文档，也不整理无关历史规格和计划。
- 不把源码字段全集、脚本 `--help` 或 Langfuse UI 说明复制到新文档。
- 不将 runbook 作为历史开发记录；它必须描述当前仍可执行的诊断流程。

## 文档边界

### `docs/observability-harness.md`

保留以下稳定内容：

1. 文档定位、事实权威顺序和 observability surfaces 的职责。
2. `trace_id`、`run_id`、`turn_id`、`delivery_id` 等关联标识的语义。
3. canonical trace、span、turn summary、delivery audit 和 operational logging 的稳定契约。
4. latency 归因原则、content capture、redaction、持久化和 fail-open 边界。
5. 长期必须成立的 invariants。
6. 关键实现入口、测试入口和诊断 runbook 导航。

移除或抽象以下内容：

- CLI 参数和命令组合全集。
- Langfuse Formatted 面板等具体 UI 表现。
- 单次 bug 修复、兼容迁移和重试实现的演进叙述。
- 已完成的 `Phase Plan` 和历史开发步骤。
- 可直接从源码、schema 或脚本帮助中获得的字段穷举。

### `docs/observability-diagnosis-runbook.md`

承载以下现役操作内容：

1. `assistant.turn: <trace_id>` 的标准识别和定位流程。
2. Langfuse、trace API、本地 canonical trace 和 Gateway lifecycle 证据的优先级。
3. Gateway、Runtime、Provider、Tool、Memory 和 delivery 的分层诊断路径。
4. 少量覆盖主要场景的典型命令。
5. trace 缺失、部分持久化、服务重启、超时或身份不匹配时的降级取证方法。
6. 输出诊断结论时区分机器事实、源码解释和推测的要求。

runbook 只引用架构契约，不重复定义事件或 schema。

## 路由调整

- `AGENTS.md` 的 trace/observability 架构路由继续指向
  `docs/observability-harness.md`。
- `AGENTS.md` 的真实测试、真实通话、真实 run/trace 和机器日志诊断规则改为优先读取
  `docs/observability-diagnosis-runbook.md`，必要时再读取 harness。
- `README.md` 并列提供观测架构和真实运行诊断两个入口。
- 其他当前权威文档只修复因拆分产生的直接引用，不做顺带重构。

## 权威与证据原则

- 当前源码和测试高于 prose；改写时必须抽查关键事件、存储和 viewer 实现。
- harness 定义稳定语义，具体字段以 Pydantic model、事件构造代码或 schema 常量为准。
- runbook 中的命令必须由当前脚本支持，并保持 mock/local/offline；文档验证不得调用真实 Provider。
- 历史 Git 记录只用于识别内容来源，不能替代当前源码事实。

## 验证

1. 确认 harness 与 runbook 的职责无重复定义或相互矛盾。
2. 核对 `AGENTS.md`、`README.md` 和当前权威文档的入站引用。
3. 对照当前脚本帮助验证 runbook 中保留的命令。
4. 运行文档证据收集器，判断缺失链接和路径是否为真实漂移。
5. 运行 `git diff --check`。
6. 本次仅修改文档，不新增或运行行为测试；若核查发现文档依赖了未验证的运行契约，再选择最小离线测试。

## 完成标准

- harness 显著短于当前 1129 行，并能独立解释稳定观测契约。
- runbook 可指导开发者从一个真实 `trace_id` 得出有证据分层的诊断结论。
- 当前权威中不再包含历史 Phase Plan、CLI 参数全集或 UI 偶然细节。
- 仓库入口能明确区分架构阅读和实际排障。
- 不触碰工作区内与本任务无关的现有改动。
