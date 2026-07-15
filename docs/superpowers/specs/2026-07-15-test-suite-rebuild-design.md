# 测试体系重建设计

## 状态

本设计已于 2026-07-15 在对话中确认并完成实施。critical 底座、八个 scope、最终 runner、
默认 pytest 入口、legacy 清理和高延迟 integration 重分类均已落地。

## 问题

仓库当前收集两千多个 pytest node。这些测试跨越项目早期开发、兼容性调整、历史回归、演示和后续架构演进。完整 offline suite 约需十二分钟，而且 Codex 在普通局部开发中也很容易触发它。

问题不只是耗时。现有套件混合了稳定契约、阶段里程碑、重复断言、历史回归、业务示例、smoke 脚本和跨层检查。marker 主要通过文件名和目录粗粒度推断，无法形成可信的“源码变更到测试范围”路由。

## 决策

采用“隔离旧套件、渐进重建”的路线：

- 当前完整套件暂时作为显式 `legacy/full-regression` 保留。
- critical 替代基线建立后，将旧套件移出普通 Codex 验证和裸 pytest 默认收集范围。
- 围绕稳定架构 scope 和明确测试层级重建测试。
- 只有当旧测试保护的行为已经移除，或已由具名保留测试承接，才按 scope 删除旧测试。
- 完整 legacy suite 仅在发布前、重大跨 scope 架构改动后或用户显式要求时运行。

这不是一次性删除。它要尽早获得速度收益，同时避免丢失现阶段仅存在于旧测试中的兼容、安全和历史故障证据。

## 目标

- 让普通 Codex 验证与实际变更范围相匹配。
- 建立几十秒内完成的全仓 critical 安全底座。
- 每个稳定行为只有一个权威测试层。
- 让源码到测试的路由显式、确定且可审查。
- 迁移期间保留唯一的安全、身份、兼容、失败恢复和历史回归证据。
- 建立长期规则，阻止 phase 测试、汇总型测试文件和跨层重复断言再次累积。

## 非目标

- 不以测试数量或覆盖率百分比为目标。
- 默认验证不启用真实 Provider 或 integration 测试。
- 首版不实现覆盖率驱动或依赖图驱动的测试选择。
- 第一迁移阶段不增加 CI 硬门禁。
- 不机械重写所有旧测试；无法对应当前受支持契约且没有历史价值的测试可以删除。

## 目标目录

```text
tests/
  critical/
    contracts/
    safety/
  scopes/
    prompt/
    context/
    tools/
    gateway/
    runtime/
    memory/
    providers/
    api/
  unit/                     # 迁移期间视为 legacy
  contracts/                # 迁移期间视为 legacy
  test_*.py                 # 迁移期间视为 legacy
  scope-map.toml
scripts/
  run_scoped_tests.py
```

`tests/critical/` 只保存所有变更都必须守住的跨领域不变量。`tests/scopes/<domain>/` 保存某一架构范围的权威测试。现有根目录、unit、contract、phase、eval、demo 和 smoke 测试在迁移或删除前均属于 legacy suite。

迁移结束后可以永久保留领域目录，不需要恢复旧的根目录式组织。

## 测试层级

每个受支持行为选择一个权威层级：

- `unit`：纯函数、状态转换、策略和数据模型。
- `contract`：Tool、Provider adapter、Memory service、Gateway frame 等稳定接口。
- `runtime`：编排、状态流转、取消、失败恢复和跨组件协作。
- `api` 或 `gateway`：协议映射、鉴权、序列化、生命周期和传输行为。
- `e2e` 或 `integration`：少量关键链路；真实 Provider 继续显式 opt-in。

同一行为只有在不同层保护不同边界时才能跨层测试。例如 Tool contract 验证结构化 `ToolResult`，API 测试只验证该结果的 HTTP 表示，不能再次完整断言 Tool 内部字段。

## Critical 底座

首批 critical suite 只覆盖系统级不变量：

- offline 默认行为、Provider 选择安全和密钥隔离。
- Tool validator、executor、registry、policy 和 audit 边界。
- RequestIdentity 与 Memory read/write policy。
- Gateway frame 契约，以及 session、cancel、interrupt 和 terminal 生命周期。
- Runtime 结构化结果、失败恢复和安全 Tool observation。
- Prompt/context 不注入 raw Provider payload、secret 或越权 Memory。

完整 critical 底座在仓库标准本地环境中的运行目标是不超过 30 秒。运行时间只用于决定验证层级，不能单独授权删除测试。

## Scope Map 与 Runner

`tests/scope-map.toml` 是源码到测试路由的机器可读权威。每个 scope 声明源码路径和权威测试路径。

示例：

```toml
[[scope]]
name = "gateway"
source_paths = [
  "src/assistant_agent/gateway/",
  "src/assistant_agent/realtime/",
]
test_paths = ["tests/scopes/gateway/"]
```

`scripts/run_scoped_tests.py` 提供三个稳定入口：

```bash
python scripts/run_scoped_tests.py --scope tools
python scripts/run_scoped_tests.py --changed BASE..HEAD
python scripts/run_scoped_tests.py --full
```

- `--scope` 运行 `tests/critical/` 和指定 scope。
- `--changed` 把 Git 变更映射为 scope，运行 critical 与所有受影响 scope 的并集。未知源码路径保守失败并给出可执行提示，不能静默跳过测试。
- `--full` 强制 mock/offline 环境并运行 critical 与全部 scope，排除 integration。

Runner 执行前打印选择出的 scope 和 pytest 路径，且绝不启用真实 Provider。

## 默认执行策略

普通局部开发运行：

```text
tests/critical + every affected scope
```

共享 runtime 或跨领域改动运行 critical 与所有受影响 scope。完整 legacy suite 仅在以下场景运行：

- 发布或合并里程碑明确要求。
- 改动跨越多个架构 scope，或修改共享测试基础设施。
- 用户显式要求 full regression。

critical 底座就绪后，裸 `pytest` 只收集 `tests/critical/`。Codex 的普通验证通过 `run_scoped_tests.py` 路由；不能仅因为修改了测试文件就自动要求完整 offline pytest。

## 测试编写规则

每个新增测试必须能说明：

1. 它保护哪个稳定契约或历史故障。
2. 为什么现有测试没有覆盖该边界。
3. 它属于哪个架构 scope 和权威 layer。
4. 失败时能否直接定位受损行为。
5. 如何处理时间、随机值、网络和全局状态。

其他规则：

- 禁止新增 phase 编号或汇总型测试文件。
- 只有 setup、边界、失败模式和断言等价时才参数化。
- 保留聚焦的故障定位，不把无关场景合并成大测试。
- 历史回归测试必须通过名称、docstring 或相邻注释说明兼容或事故目的。
- 时间行为使用注入时钟或 fixture；禁止写入随真实时间推移而失效的日期。
- 使用确定性 ID 和本地 fixture；可通过时钟或事件表达的行为不使用真实 sleep。
- 默认 Provider 始终是 mock/local/offline。
- 新增 regression 时同步判断它是否替代旧测试，并在同一变更中删除冗余测试。

## 迁移流程

### 第一阶段：路由基础设施

- 新增 scope map 和 scoped runner，并用临时 Git/pytest 仓库测试。
- 新增 critical 与 scope 目录。
- 在 `tests/README.md` 记录命令，在 `AGENTS.md` 增加简短 Codex 路由。
- 初始 critical 底座完成前，不改变当前 pytest 默认行为。

### 第二阶段：Critical 底座

- 从当前测试中盘点 critical 安全不变量。
- 移动、重写或保留保护这些不变量的最窄测试。
- 独立验证 critical suite，并保持在运行预算内。
- 只有 critical 底座通过后才切换裸 pytest 默认收集范围。

### 第三阶段：Scope 迁移

迁移顺序：

1. Tool calling 与 Provider safety。
2. Gateway、API 与 realtime protocol。
3. Runtime、streaming 与 agent routing。
4. Context、prompt 与 conversation history。
5. Memory。
6. 商品、图片、视频及其他业务 adapter。
7. Eval、demo、smoke 与旧 phase regression。

每个 scope 都执行：

```text
受支持行为清单
  -> 旧测试分类
  -> 具名 critical/scope 保留测试
  -> 新旧测试对照验证
  -> 删除完全覆盖或失效的旧测试
  -> 更新 scope-map 与测试文档
```

### 第四阶段：移除 Legacy

- 删除剩余失效、纯阶段型和重复测试。
- 删除临时 legacy 路由。
- 将 critical 与所有 scope 的并集定义为新的完整 offline suite。
- integration 和真实 Provider 测试继续显式 opt-in。

## 失败处理

- 源码变更无法映射到 scope 时视为路由错误，不能静默跳过。
- scope 目录缺失或为空时给出明确错误。
- scope 测试失败时停止 runner，并原样传播 pytest 退出码。
- legacy suite 失败与 selected critical/scope 结果分别报告。
- 迁移期间证据不足的旧测试继续保留，直到行为和历史被确认。

## 文档权威

`tests/README.md` 继续作为测试结构、编写规则和命令的人类可读权威。`tests/scope-map.toml` 是机器可读路由权威。`AGENTS.md` 只保留简短入口并指向这两个文件。

本设计落地后不再新增重复的测试架构文档。

## 验收标准

- 切换门槛满足后，裸 pytest 只运行 critical 底座。
- scoped 命令运行 critical 与指定 scope，且不夹带无关 scope。
- Git range 选择稳定、打印决策，并对未映射源码变更保守失败。
- 迁移期间完整 legacy 可在 mock/offline 环境显式运行。
- critical、scope、legacy 和 integration 结果可明确区分。
- critical 覆盖列出的安全不变量并满足运行目标。
- 每个被删除的旧测试都有具名保留测试或明确的行为移除说明。
- Runner 测试覆盖单 scope、多 scope、未映射路径、非法 Git range、pytest 退出码传播、offline 环境强制和 tracked file 不变性。

## 实施结果

- `tests/critical` 保留 22 个文件、199 个 node；裸 pytest 实测 4.7 秒。
- 八个 scope 保留 226 个权威测试文件；单 scope 实测约 2–40 秒。
- 删除 69 个 legacy 文件，主要包括 phase 汇总门禁、旧 intent/router/planner 路径、
  demo/eval/smoke 脚本自测和重复文档门禁。Phase 8 的真实 ReAct 契约改为语义命名后保留。
- proactive wake coordinator、delivery 和 SQLite store 的高延迟并发/恢复证据没有删除，
  重分类到显式 opt-in 的 `tests/integration`。
- integration 共保留 7 个文件，默认 critical、scope 和 `--full` 都不会启用真实 Provider。
- 最终完整 offline suite 为 2061 个测试与 6 个 subtest，实测约 4 分 13 秒；普通开发不运行它。

删除证据按候选类别记录如下：

| 候选类别 | 处理与保留证据 |
| --- | --- |
| `test_phase0_*` | 汇总门禁删除；Gateway、Tool、Memory 和 redaction 不变量分别由 `tests/critical/test_gateway*.py`、`test_tool_*.py`、`test_memory_*.py` 和 scope 权威测试承接。 |
| `test_phase1_*` | 汇总门禁删除；realtime lifecycle、cancel、arbitration 由 `tests/scopes/gateway/` 承接。 |
| `test_phase2_*` | 汇总门禁删除；Memory manager、read/write policy、retrieval eval 由 critical 与 `tests/scopes/memory/` 承接。 |
| `test_phase3_*` | 汇总门禁删除；Skill loader/catalog 和 native tool contract 由 context/tools scope 承接。 |
| `test_phase4_*`、`test_phase5_*` | 汇总门禁删除；multi-agent routing 与 observability 由 runtime scope 的具名测试承接。 |
| `test_phase6*`、`test_phase7*` | 阶段文档/API 汇总删除；当前 API contract 由 `tests/scopes/api/` 承接。 |
| `test_phase8a1_*`、`test_phase8a2_*` | 不删除行为；重命名为 `test_react_action_quality.py` 和 `test_react_final_answer_handoff.py` 后迁入 runtime。 |
| intent/router/planner/rule-router 旧路径 | 按仓库架构边界移除其测试权威；真实 LLM 路径不再依赖旧 intent/router/plan 选择工具。 |
| demo/eval/smoke 与脚本 import 汇总 | 删除重复入口自测；Provider opt-in/redaction 由 `tests/scopes/providers/test_provider_opt_in_safety.py`，领域契约由各 scope 承接。 |
| 文档门禁、旧目录 layering、entry cleanup | 由 `tests/critical/test_scoped_test_runner.py` 的最终目录、文档路由、全源码映射和无 legacy 路径断言承接。 |
