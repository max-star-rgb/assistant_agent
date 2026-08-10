# Agent Eval 生产 Registry 与精确替换设计

## 1. 背景

当前 Agent eval 先用运行配置创建默认 Tool Registry，再无条件追加一套受控高德 MCP Tool。
当正式 Experiment 以 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 运行且本机已配置高德 MCP 时，
默认 Registry 已包含 `mcp.amap_maps.maps_geo` 等真实 Tool，追加同名受控 Tool 会触发
`Tool already registered`，导致 Environment 在 Agent 执行前失败。

这个故障还暴露了更深的装配问题：正式 Experiment 应以生产 Registry 为事实源，Task 的受控依赖
只能精确替换已有 Tool，不能在生产目录之外再拼一套平行目录。

## 2. 已确认目标

- 正式 Langfuse Agent Experiment 默认使用真实 Chat Provider、生产 Tool Registry、真实外部依赖和
  真实读写副作用。
- 服务以 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 启动，视为 operator 已授权该服务触发的正式
  Experiment 使用已配置真实 Tool；Langfuse UI 每次运行不再增加第二个副作用确认字段。
- Task 可以为故障恢复、固定证据或人工校准精确替换少量 Tool；未声明替换时，所有 Tool 保持生产实现。
- 替换必须保持 Agent 可见的 Tool 身份和契约不变，并在 Registry seal 前原子完成。
- `--inspect`、pytest 和不执行 Agent 的契约检查继续保持离线；正式 `--run` 才装配 live Registry。

## 3. 非目标

- 不新增按用户自然语言或 Task capability 裁剪工具的规则。
- 不在 Langfuse Dataset metadata、grader 或用户请求中保存替换配置。
- 不让 Langfuse UI 扩大服务启动时没有获得的 Tool 权限。
- 不为真实 Tool 提供静默 mock fallback；真实配置或 readiness 不完整时 fail closed。
- 不改变 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool` 治理链。

## 4. 总体架构

正式运行使用一条装配路径：

```text
ProviderConfig(real) + 生产依赖
  -> 生产 composition root
  -> 完整生产 Tool Registry
  -> Task 精确 replacement overlay（默认空）
  -> 校验、注册、seal
  -> AgentGraphRuntime
  -> 真实 Tool 调用与真实状态
  -> Evidence / Rule / Langfuse Evaluator
```

生产 Registry 是工具集合、ToolSpec、权限和 readiness 的唯一事实源。eval 不再无条件调用
`add_controlled_amap_tools()`，也不再凭部署 allowlist 创建生产 Registry 中不存在的工具。

默认 replacement 集合为空，因此默认行为就是完整生产端到端运行。方案同时覆盖两种需要：

- 生产镜像 Task：空 replacement，所有 Tool 都是真实实现；
- 受控依赖 Task：只替换明确列出的既有 Tool，其余目录仍是生产实现。

## 5. Registry overlay 契约

### 5.1 Environment 声明

`ControlledTaskEnvironment` 的事实含义调整为“Task 拥有依赖模式与证据边界”，不再等同于“所有依赖
都是 mock”。Environment 通过专用 hook 返回结构化 replacement 声明；默认返回空集合。

每条声明至少包含：

- `tool_name`：必须是生产 Registry 中已注册的规范名称；
- `replacement`：替换后的 Tool 实例；
- `reason`：稳定、可审计的替换原因；
- `source_ref`：Git 中的 Task/共享 fixture 来源；
- `dependency_mode`：固定为 `controlled_replacement`。

replacement 配置只存在于 Git Environment 代码中，不进入 `task.json`、用户请求、Dataset metadata
或 Evaluator prompt。

### 5.2 原子替换

overlay 不修改已 seal 的生产 Registry，而是以生产 Registry 为输入创建新的 Registry generation：

1. 读取生产 Registry 中每个已注册 Tool 和 registration record；
2. 对未声明 replacement 的名称复用生产 Tool；
3. 对声明 replacement 的名称放入替换 Tool；
4. 完整验证后一次性注册并 seal；
5. 任一检查失败时不产生半装配 Registry。

禁止“先复制全部生产 Tool，再追加受控 Tool”。一个规范 Tool name 在最终 Registry 中始终只有一个
实现。

### 5.3 替换校验

Environment validation 必须在 Agent 执行前证明：

- replacement name 唯一且是生产 Registry 的子集；
- `replacement.name == tool_name`；
- replacement 与原 Tool 的模型可见 `ToolSpec` 完全一致，包括 input schema、category、媒体要求、
  repeat policy 和其他权限字段；
- replacement registration 明确记录原始 production registration 与 replacement provenance；
- required tool、实际可见 catalog 与 outcome expectation 仍一致。

要求 ToolSpec 一致的目的是只替换执行依赖，不改变模型看到的工具名称、描述、参数或权限，避免
Environment 暗中降低选择难度。

## 6. Runtime 与真实副作用

正式 `--run` 不再把业务运行配置强制改写为 memory/local/offline 版本。Runtime 使用与生产装配相同的：

- MCP 配置和认证；
- Calendar、shopping、website、memory 等已启用 backend；
- durable task / durable workflow service 与持久化 store；
- Plugin 配置、readiness 和 Tool allowlist。

eval 仍可以附加 Evidence collector 和 Langfuse trace context，但这些观察组件不能替换业务依赖或改变
Tool 执行链。

真实写操作不自动回滚或清理。每个 Experiment Item 使用唯一 `session_id/run_id`，并在 Trace、
Workflow owner 和支持 metadata 的 Tool 中保留 `task_id + experiment run` 关联。不得通过修改用户业务
字段来偷偷添加测试标记；需要外部资源可检索标记的 Task，应把该标记作为自然请求的一部分明确设计。

`real` 模式只授权已经通过生产配置、Plugin readiness、MCP allowlist 和 Validator 暴露的能力，不能
让 UI payload 注册新 Tool、修改凭据或扩大 allowlist。

## 7. Evidence、失败归因与评分

Evidence 增加非模型可见的依赖来源投影：

- Registry generation；
- 每个实际调用 Tool 的 `dependency_mode=live|controlled_replacement`；
- production source 与 replacement source；
- Tool 的结构化成功、错误码和终态。

失败分为两层：

1. Registry 发现失败、配置缺失、replacement 非法、Environment 无法读取终态等发生在 Agent 执行前
   或 Evidence 构造阶段的问题，属于 infrastructure failure，不生成 Agent Score；
2. Agent 已开始运行后，真实 Tool 的成功、业务拒绝、权限错误、超时或 Provider 错误都是本次生产式
   端到端行为证据，按 Task outcome contract 和恢复要求进入 `task_conformance`、`grounding` 与
   `response_quality`，不得静默换成 mock 重跑。

Score 仍保持现有三个 canonical task-level BOOLEAN，不新增总分。Trace/comment 应显示 live 或
replacement provenance，使人能够区分 Agent 决策错误、真实依赖失败和故障恢复表现。

## 8. Task 迁移

### 8.1 Deep Research Mission

`deep_research_autonomous_admission`、`deep_research_constraint_grounding` 和
`deep_research_evidence_plan` 默认不声明 replacement：

- 高德等无关 Tool 来自生产 Registry，但 Agent 正常不应调用；
- `workflow_submit` 使用生产 WorkflowService 和持久化 store；
- objective state Rule 从该真实 store 读取当前 run 创建的 Workflow；
- 不再创建 `InMemoryWorkflowStore` 或临时 Workflow artifact 路径。

这样本次 `maps_geo` 重复注册问题自然消失，因为 eval 不再追加第二套高德 Tool。

### 8.2 确定性故障 Task

例如 `amap_weather_provider_failure_recovery` 可以显式把生产 Registry 中的
`mcp.amap_maps.maps_weather` 替换为保持相同 ToolSpec、固定返回 `provider_timeout` 的实现。该 Task
仍验证确定性恢复行为，但其他 Tool 保持生产实现。

### 8.3 固定答案 Task

依赖固定酒店、路线、网页或天气内容才能成立的 Task 必须逐项审计：

- 若能力目标允许动态真实数据，删除 replacement，并把 Rule 改为验证结构化事实与回答 grounding；
- 若能力目标本身是故障注入或固定证据推理，保留精确 replacement；
- 不允许为了让旧 calibration 文本继续通过而替换与能力无关的工具。

## 9. 离线检查与正式验证

### 9.1 离线层

- `--inspect` 不连接真实 Provider/MCP，只检查 Task、replacement 声明形状、静态名称和 Mission Rule；
- pytest 使用 mock/local/offline production-shaped Registry，验证空 overlay、同名替换、未知名称拒绝、
  ToolSpec 不一致拒绝和原子失败；
- calibration 使用已登记 Evidence 样本校准 Evaluator，不通过运行真实 Tool 构造样本。

### 9.2 正式层

正式验证按以下顺序执行：

1. real 模式启动 Assistant Server；
2. 单独运行 `deep_research_autonomous_admission`；
3. 确认 Registry 中 `mcp.amap_maps.maps_geo` 只有一个 registration，来源为 production/live；
4. 确认 Agent Trace、真实 `workflow_submit`、持久化 Workflow 终态和三个 task-level Score；
5. 再运行一个明确声明 replacement 的故障恢复 Task，确认仅目标 Tool 标记为
   `controlled_replacement`；
6. 单 Task 通过后才扩大 ACTIVE Dataset 范围。

真实验证会产生外部调用和持久化写入，结果报告必须列明调用范围、写入资源、Trace 和验证结果。

## 10. 文档与兼容性

实现时同步修改：

- `evals/README.md`：把默认 Environment 从“完整受控目录”改为“完整生产目录 + 可选精确替换”，并
  说明 real 服务启动授权和真实副作用；
- `docs/tool-calling-architecture.md`：只补充 eval 使用生产 Registry composition root 与 seal 前
  overlay 的边界，不复制 Experiment 操作步骤；
- `.codex/skills/langfuse-eval-engineering/SKILL.md`：只更新 workflow 检查清单和权威文档链接措辞；
- `scripts/README.md`：只保留命令用途和副作用提示。

当前 Task Environment API 可以保留一段迁移兼容，但正式 `--run` 不得继续调用会无条件追加高德目录
的旧 `build_controlled_registry()`。迁移完成后删除该正式运行路径，避免双重事实源。

## 11. 验收标准

- real 模式且配置高德 MCP 时，`deep_research_autonomous_admission` 不再出现重复 Tool 注册；
- 空 replacement 的最终 Registry 与生产 Registry 具有相同 Tool names、ToolSpec 和 live backend；
- 精确 replacement 不改变最终 Tool names 和模型可见 ToolSpec；
- 未知、重复或契约不一致的 replacement 在 Agent 运行前 fail closed；
- Trace/Evidence 能区分 live 与 controlled replacement；
- Deep Research 使用真实 Workflow store，并能通过当前 run 的 owner/run identity 读取目标终态；
- 正式运行不静默 mock fallback，真实 Tool 失败保留为端到端证据；
- `--inspect`、pytest 和 calibration 仍不发起真实 Provider 或外部 Tool 调用；
- 文档只在各自权威位置展开契约，避免重复操作说明。
