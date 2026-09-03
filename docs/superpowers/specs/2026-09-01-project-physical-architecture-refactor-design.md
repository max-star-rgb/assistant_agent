# 项目物理架构重构总纲

日期：2026-09-01  
状态：五个切面已完成首轮收口；后续变更以当前 authority 和真实消费者为准

## 1. 文档定位

本文记录 `assistant_agent` 的代码与文档物理架构诊断，以及后续重构必须遵守的总体原则和推进顺序。
它属于开发设计材料，不是当前运行时事实 authority；生产行为仍以 `AGENTS.md`、`docs/authority.toml`
路由到的专项 authority、源码和测试为准。

本次重构的目标不是改变已经清晰的逻辑架构，而是让目录、文件归属和依赖方向准确表达当前逻辑架构，降低定位、
修改和验证成本。

## 2. 宏观结论

当前目录主要表达项目的演进历史，而不是当前生产架构。

逻辑上的生产主链已经明确：

```text
LangGraph Agent Server
  -> 进程级 composition
  -> native Assistant / worker / Memory graph
  -> middleware、Tool、Provider、Memory、Media 等领域能力
```

但物理目录同时使用了三种分类方式：

- 按领域分类：`media`、`memory`、`tools`；
- 按技术角色分类：`runtime`、`providers`、`api`；
- 按演进阶段或实现形态分类：`native_agent`、`multi_agent`、`improvement`。

这些分类方式没有形成单一稳定的上位规则，导致同一职责散落、相邻职责重复命名、入口层直接依赖多个领域实现。

## 3. 事实依据

扫描时 `src/assistant_agent` 约有 64,375 行 Python，主要物理热点包括：

- `tools`：68 个 Python 文件；
- `media`：55 个 Python 文件；
- `runtime`：18 个 Python 文件；
- `providers`、`observability`、`native_agent`：各约 16 个 Python 文件；
- `multi_agent`：15 个 Python 文件；
- `context`：13 个 Python 文件。

单文件热点包括：

- `agent_server/media_app.py`：约 1,623 行；
- `config/__init__.py`：约 1,431 行；
- `media/video/realtime_video_observer.py`：约 1,400 行；
- `tools/plugins/builtin/media_inspection/video_branch.py`：约 1,294 行；
- `automation/durable_tasks/service.py`：约 1,034 行；
- `runtime/chat_adapter.py`：约 973 行；
- `native_agent/assistant_agent.py`：约 764 行。

`docs/authority.toml` 还显示多个源码文件由多个逻辑 domain 共同覆盖：

- `native_agent/assistant_agent.py` 同时关联 context、runtime、observability 和 Tool；
- `native_agent/state.py` 同时关联 context、Memory 和 runtime；
- `runtime/thread_resources.py` 同时关联 Agent Server、runtime 和 Tool；
- `native_agent/memory_middleware.py`、`runtime/generated_artifacts.py`、`runtime/local_backend.py`
  也跨越多个 authority。

跨包 import 还存在明显的双向关系，例如：

- `tools <-> media`；
- `tools <-> native_agent`；
- `runtime <-> providers`；
- `runtime <-> tools`；
- `media <-> providers`；
- `native_agent <-> runtime`。

这些关系不必全部消失，但它们说明当前物理边界无法直接表达稳定的依赖方向。

## 4. 主要问题

### 4.1 Composition root 不够集中

`agent_server/services.py` 是真实生产装配中心，但配置解析、模型构造、Tool inventory、backend、middleware、worker、
Memory graph 和媒体资源分别位于多个一级目录。阅读一次启动流程需要跨越大量包边界。

### 4.2 配置包承担过多职责

`config/__init__.py` 同时承担配置 schema、环境变量读取、Provider 选择、兼容字段、默认值推导和测试辅助判断。
包入口本身成为大型实现文件，使“配置契约”和“环境装载实现”无法独立理解。

### 4.3 `native_agent`、`runtime` 与 `context` 边界重叠

三个目录都包含 state、模型、请求、上下文、压缩或执行相关概念。当前生产已转向原生 LangGraph/Deep Agents，
但部分旧 Runtime 和上下文基础设施仍与新主链处在同一层级，难以仅凭目录判断哪些是生产核心。

### 4.4 领域实现与 Tool adapter 混合

`media` 持有视觉和视频领域实现，`tools/plugins/builtin/media_inspection` 又包含较重的视频理解编排。
Tool adapter 与领域服务之间缺少足够明显的物理边界。

### 4.5 现役代码与非生产代码并列

以 `agent_server.graph`、`agent_server.auth` 和 `agent_server.media_app` 为入口的静态 import 可达性分析显示，
`improvement`、多数 `multi_agent`、部分旧 `context`、旧 Provider adapter 和 observability 模块不在生产主链上。
该分析不包含所有脚本、eval 和动态导入，因此不能直接作为删除依据，但足以说明后续必须先分类再移动。

### 4.6 文档逻辑治理有效，物理体积失衡

当前 authority 文档数量有限且路由清楚；`docs/superpowers` 中大量历史 spec/plan 已被规则明确排除在当前 authority 之外。
因此历史文档不是首批重构对象。只有当源码 owner 或 authority 边界变化时，才同步修改当前文档路由；不为视觉整洁
机械搬迁历史材料。

## 5. 重构原则

1. **生产主链优先**：先让启动、装配和核心执行路径清晰，再处理外围模块。
2. **一个文件一个主要 owner**：允许引用多个领域，但不应由多个 authority 共同定义其主要职责。
3. **稳定依赖方向**：入口依赖 composition，composition 依赖领域契约，领域实现依赖外部 adapter；避免反向依赖。
4. **先分类，后移动**：每个模块先标记为生产核心、领域实现、外部适配、脚本/eval 支撑或删除候选。
5. **删除优先于归档**：确认无调用、无协议责任、无测试价值的代码直接删除，不建立新的 `legacy` 垃圾场。
6. **移动优先于重写**：目录调整阶段尽量不改变行为，避免把架构迁移与功能重写混在一起。
7. **不套用通用四层模板**：除非现有边界无法承载，不机械新增 `core/domain/application/infrastructure`。
8. **渐进兼容**：只有真实外部 import 需要迁移窗口时才保留薄 re-export；内部 import 应在同一切面直接更新。
9. **每个切面独立闭环**：设计、移动、测试、authority 更新和提交在一个小范围内完成。
10. **源码和测试优先**：文档随已验证的物理边界更新，不通过文档命名掩盖源码现实。

## 6. 推进顺序

### 切面一：生产启动与装配

范围：`config`、`agent_server/services.py`、`agent_server/graph.py`、`native_agent` 的 composition 接口。

目标：明确配置契约、环境装载、进程资源 owner、Graph factory 和 middleware 组合之间的物理边界。

### 切面二：Agent 执行与上下文

范围：`native_agent`、`runtime`、`context`。

目标：区分当前原生 Agent 核心、仍被生产消费的共享契约，以及旧 Runtime/Context 候选。

### 切面三：Tool 与领域能力

范围：`tools/plugins`、`media`、`automation`、`memory`、`providers`。

目标：让 Tool 保持薄 adapter，业务编排归领域服务，Provider 只承担外部能力适配。

### 切面四：外围与非生产模块

范围：`multi_agent`、`improvement`、`evaluation`、旧 `api/clients`、非生产 observability。

目标：逐项确认保留、迁移、仅供脚本/eval 使用或删除，不整体建立新的归档包。

### 切面五：文档和导航收口

范围：`docs/authority.toml`、当前 authority、`docs/README.md`、必要的包级说明。

目标：使 source globs、owner 和最终源码目录一致；历史材料继续保持非 authority 身份。

## 7. 变更边界

本轮重构不改变以下产品和运行时决策：

- 不改变统一 `native_agent.AssistantAgent` 生产主链；
- 不新增第二套 Agent Runtime、Tool Registry 或执行器；
- 不改变 mock/real Provider 安全边界；
- 不绕过 ToolNode、ToolRuntime、原生 HITL、Memory middleware 或 Agent Server 生命周期；
- 不串行化或删除视觉 authority 定义的并行感知流水线；
- 不借目录重构修改产品功能、Prompt 行为或外部协议；
- 不在重构期间批量清理历史文档。

## 8. 验收标准

完成全部切面后，应满足：

- 从 `langgraph.json` 可以沿单一、清楚的路径定位生产 composition；
- 一级包名能够说明领域或架构职责，不再混用演进阶段名称；
- 配置 schema、环境读取和资源构造可以分别定位；
- 生产核心文件原则上只有一个主要 authority owner；
- Tool adapter 不承载可独立存在的领域编排；
- 非生产模块具有明确保留理由，否则被删除；
- 不依赖长期兼容 shim 维持仓库内部 import；
- 每个切面完成后，相关 core invariant、临时 TDD、authority validator 和 8089 热重载验证均通过。

## 9. 首个深入切面

第一切面从生产启动与装配开始。分析顺序固定为：

1. `ProviderConfig.from_env()` 的 schema 与加载职责；
2. `AgentServerExecutionOwner.compose()` 的资源 ownership 和装配职责；
3. `build_assistant_agent()` / `build_general_purpose_worker()` 的 Graph factory 职责；
4. `langgraph.json` 和 `agent_server/graph.py` 的薄入口职责；
5. 对应 authority 与测试入口。

在该切面设计获得确认前，不移动源码文件。

第一切面的已确认详细设计见
[`2026-09-01-production-composition-config-refactor-design.md`](2026-09-01-production-composition-config-refactor-design.md)。

## 10. 首轮实施结果（2026-09-03）

五个切面已经按“小范围迁移或删除、同步 authority、独立验证”的方式完成首轮收口：

- 生产 composition、配置 schema/env 装载与原生 Graph factory 已分别定位；
- 旧 Runtime state、wrapper graph、平行执行契约和无消费者 Context/Runtime 兼容层已删除；
- 本地 Tool 已统一到原生 `BaseTool -> ToolNode -> ToolRuntime`，领域实现继续由各自 authority 管理；
- 旧 `clients`、`api`、pilot identity/trial、断联 improvement lab、旧 evaluation contract，以及无消费者的
  observability 投影与未接线 wrapper 已删除；媒体 callback 已归位 `agent_server`，multi-agent response DTO 已归位
  `multi_agent`；
- `DEFAULT_AGENT_ID` 已归位共享 `identity.py`，生产与外围包不再反向依赖可选 `multi_agent`；
- 当前 authority、README 导航和源码 owner 已随每个子切面更新，历史材料继续保持非 authority 身份。

首轮停止边界不是“所有一级包越少越好”。当时 `multi_agent` 仍承担明确声明的可选 A2A 协议，`context` 与剩余
observability trace store/query 需要继续逐项核实消费者，不能仅凭生产主链不可达直接删除。

## 11. 追加收口结果（2026-09-03）

在逐符号消费者审计、临时 TDD、独立评审和 mock/offline 验证后，第二轮继续完成：

- 通用 token counter 归位 `native_agent/token_counter.py`，生产上下文不再从可选包反向导入；
- 视觉窗口实际使用的 token policy 与 Provider usage normalize 归位 `media/video/token_budget.py`；无消费者的旧
  `TokenBudgetReporter`、`TokenBudgetEstimate` 和 metadata 估算路径删除；
- 零消费者的 context report 构造、v1/v2 转换及 TraceQuery context/tool-call 查询删除；
- 唯一仍存活的 `RealtimeVideoContext` DTO 归位 `media/video/realtime_video_memory.py`，随后删除整个
  `src/assistant_agent/context/` 一级包；
- observability 兼容层收窄为可选 multi-agent 实际消费的 run/trace summary，以及 visual eval 直接消费的
  `TraceStore`。

该轮停止边界位于视觉 authority 明确保留的独立兼容代码：`VisualContextService`、旧视觉 summary compactor、
semantic store 中的 summary 状态和相关配置当时未进入生产 realtime observer，但仍被文档声明为专项兼容能力，
因此等待明确退役决策。

## 12. 经批准的视觉兼容链退役（2026-09-03）

用户批准退役后，逐符号审计确认该链没有生产、脚本或当前测试消费者，随后删除：

- `visual_context.py`、`visual_context_compactor.py`、`visual_context_models.py`；
- semantic store 的 revisioned summary、covered-record 状态和 CAS API；
- `visual_context.*` 兼容观测事件；
- 仅供旧 service 使用的 recent-record 与 instruction/image/output reserve 配置。

仍保留 `visual_memory_search` 使用的 `VisualTimelineContextService`、timeline compactor、共享 token policy 和
target/trigger/hard 配置；它们不持久化 summary，也不改变并行关键帧 VLM 或 exact-target barrier。
