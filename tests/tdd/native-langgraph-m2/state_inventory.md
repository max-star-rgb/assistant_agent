# AssistantTurnGraph checkpoint state inventory

本清单以 M1 真实 `AssistantLoopState`、三个 node、route 与 Runtime prepare/finalize 为输入，定义
Task 1 的恢复闭包。目标不是把 `AgentState` 原样 JSON 化，而是把 node 间必须延续的执行事实逐字段
投影到 `AssistantTurnState`；Provider、Tool 与服务对象只允许通过 `GraphRuntimeContext` 注入。

## 现有 channel 与读写位置

| 现有字段 | 读取者 | 写入者 | Task 1 持久映射 |
| --- | --- | --- | --- |
| `request` / `state.request` | assistant、context build/preflight、validator、Tool、compose、Runtime finalize | preflight 会替换 request；loop/usage/validator 会写 metadata | `PersistedRequest` 的显式字段、`messages`、`runtime_task_facts`、`media_refs`；loop 所需的结构化事实拆到专用字段，禁止 generic metadata |
| `state.run_id/trace_id/agent_id/status/errors` | 全部 node、事件/trace、route、Runtime finalize | Tool/compose/cancel/error branches | `PersistedRun`；error 仅保留安全 code/message/source，不持久化任意 details |
| `state.session_memory_snapshot` / `frozen_memory_context` / `memory_context_prepared` | context build/preflight | Runtime memory preparation | memory item 的稳定 ref/安全摘要进入 `context_refs`；运行期 frozen 对象由 Runtime context/服务按 ref 重建，不进 checkpoint |
| `state.context_source_result` | context build | Runtime prepare | `context_refs` 中显式 source/section ref、version、issue code；正文由 context service 重建 |
| `state.perception` | context build / Tool qualification | Runtime/media preparation | `PersistedMediaRef` 与 `context_refs`；视觉正文、bytes、provider raw result 不持久化 |
| `state.capability_grants` / `session_restored_grant_ids` | context Tool catalog | capability controller | grant 的稳定 `capability_refs`；grant 对象和 ToolSpec 由 runtime service 重建 |
| `state.run_tool_catalog` | validator、context compiler | context build | `PersistedRunToolCatalog`（schema version、available names、selection/exclusion reason codes） |
| `state.tool_calls` | budget/loop guards/trajectory/compose | governed ToolExecutor | `PersistedToolCall`（identity/name/status/timestamps/output ref/error summary/input 的显式 bounded JSON value） |
| `state.tool_results` | compose/trajectory | governed ToolExecutor | `PersistedToolResult`（status/safe observation/operation key/output+artifact refs）；禁止 `data/audit_payload/raw response` |
| `state.response` | route、compose、Runtime finalize | Tool handoff/compose | `PersistedResponse`（message、followup、output refs、URL citations）；不得含 artifact body |
| `outputs_by_step` | graph execution result/compat consumers | Tool node | ordered `PersistedStepOutput` tuple，仅 step id、Tool/result status、refs、safe observation |
| `current_step_index` | compatibility plan facts | Runtime input | bounded integer channel，new turn 重置为 `0` |
| `assistant_output` | route、Tool node、compose/trajectory | assistant node | discriminated `PersistedAssistantOutput`；Tool input 使用严格 bounded JSON value，不保存 provider raw payload |
| `turn_origin_id` | assistant Tool operation identity | Runtime new turn | 当前逻辑 turn 首次 `run_id`；resume 更换 invocation `run_id` 时保持不变，new turn 必须覆盖 |
| `pending_tool_calls` | assistant、Tool batch | assistant/Tool nodes | `PersistedToolCallRequest` tuple；在 Tool edge 前包含 provider correlation 与稳定 `operation_scope_id`；new turn 重置为空 |
| Tool/assistant counters 与 `run_phase` | assistant、Tool budget、route | assistant/Tool nodes | 独立 primitive channels；new turn 全部重置 |
| `tool_observations` | context/assistant/trajectory | Tool node | `PersistedToolObservation` tuple，只保留 prompt-safe summary/status/error/refs/bounded detail |
| stream boundary facts | Provider delta adapter | assistant/Tool nodes | 三个显式 bool channel；恢复时保持当前 turn，new turn 重置 |
| `last_llm_span_id/attempt_kind` | trace correlation | assistant node | 独立 bounded string；new turn 清空；不是调度身份 |
| `max_*` budgets | assistant/Tool | Runtime input | 明确非负整数 channel；profile 后可收窄 |
| `pending_interrupt` | native `await_input` node 与 resume preflight | Runtime trusted input / `await_input` resume | strict nullable `approval|input` DTO，含安全 prompt、稳定 `action_ref` 与非空 allowed resume kinds；interrupt 前先进入 checkpoint，成功 resume 原子清空，new turn 为 `None` |

## node 边界

- `assistant_node`：adapter 从 strict DTO hydrate 临时 `UserRequest`/`AgentState`，Runtime context 注入
  adapter、executor、context/trace/event/cancel 服务；node 的所有原地修改在返回前逐字段 project 回 DTO。
- `execute_requested_tool_node`：仅临时构造治理链需要的 `AgentState` 与 `ToolResult`；执行后只投影调用
  lifecycle、safe observation、operation/output/artifact ref 和 error summary。`ToolResult.data`、audit payload、
  registry/tool 实例均不得返回给 graph。
- `compose_response_node`：临时 hydrate 后复用 response composer，返回 strict response/run terminal facts。
- `route_after_assistant`：只读取 strict status 与 discriminated assistant output，不 hydrate runtime dependency。
- Runtime finalize：从最终 strict state hydrate 外部兼容 `AgentState`，继续维持既有 public API、Agent-Service
  和媒体交付行为；hydrate 对版本不匹配与缺字段 fail closed。

## M5 Runtime public surface inventory

`AgentGraphRuntime` 的稳定公开方法收缩为
`initialize_session_memory`、`run_state`、`arun_state`、`astream_state`、
`aresume_state`、`areplay_state`、`afork_state`、`drain_memory_ingestions`、
`run_task_quantum` 与 `close`。仓库没有 `run()` 的真实调用方，终态兼容统一通过
`run_state` / `arun_state` 表达；history 与 thread retention 由持有 compiled app 的
`AssistantRuntimeApp` / `RuntimeHost` 直接治理，不再扩张 Runtime facade。

legacy retirement gate 已闭合，`run_work_item` 与 `workflows/execution.py` 均已删除；不得新增
compatibility alias。

## Runtime-only 重建项

`ToolExecutor`/Registry、`ChatAdapter`/chat callback、`ContextService`/projector、memory/media/artifact stores、
event sink、trace store/callback、cancel token、database connection 与 capability controller callback 全部由
`GraphRuntimeContext` 注入。它们不能出现在 DTO、nested DTO、checkpoint channel 或任意 metadata/data
逃生舱中。

Task 1 的恢复边界不虚构尚不存在的 context/memory/perception 历史 snapshot store：

- `GraphRuntimeContext.state_ref_resolver` 是显式 resolver protocol；默认实现把 freshly prepared
  `context_refs`、`capability_refs` 与 checkpoint 精确比较，缺失、版本变化或 grant 扩缩都在 Provider/Tool
  调用前 fail closed。
- checkpoint catalog 是执行事实；node hydrate 前先 apply 到 fresh `AgentState`，同时要求其中每个可用 Tool
  都仍存在于 runtime Registry。Task 3 profile 会继续收窄 policy，不在 Task 1 推断新 catalog。
- 真正跨部署恢复 context/memory/perception 正文仍需要后续领域 snapshot resolver/store；在该能力存在前，
  ref 不一致不得静默重算或采用变化后的快照。

Tool observation 的 model-visible detail 先经 `sanitize_tool_observation_detail`，再通过 bounded
`PersistedObservationDetail` 的 exact scalar allowlist 显式投影；nested/list/dict、未知字段以及
credential/token/raw/provider/media body/path 等 key 拒绝进入 checkpoint。复杂 observation 若是恢复必要事实，
必须由后续领域 store 提供稳定 `observation_ref` 并经 resolver 加载，不能回退成 arbitrary `data`/metadata
逃生舱。

`CapabilityOutputContract` 通过 `PersistedCapabilityContract` 单独持久化 capability/status/contract output ref、
error code/message/recoverable，以及少量正向允许的 scalar data/metadata。`ToolResult.output_ref` 与
contract-owned `output_ref` 保持两个语义字段，不互相提升；恢复后 response 的 public contract projection 与
不中断路径等价。所有 string scalar 与 ref 还经过 checkpoint-local 正向 validator：ref 只接受安全 public
HTTP(S) URL 或 `artifact/memory/media/output` opaque stable scheme；userinfo、signed/credential query、
data/file URI、绝对路径、credential/body 文本均拒绝。该 validator 不依赖 LangSmith redactor。

Tool call 与 pending provider call 的 argument 使用同一 checkpoint-local sanitizer 递归验证 bounded JSON；
敏感 key/value、AWS/GCS/OSS/Azure SAS 签名 URL、嵌入式认证、data/base64 body、私有 Unix/Windows 路径、
相对媒体路径、过深/过大结构与长正文一律 fail closed，不允许通过 drop 改变 Tool 语义。普通 public URL、
bounded query、stable opaque ref 和用于 ActionValidator/模型 repair 的空 JSON string 保持原值。自由用户文本
不进入该执行事实 sanitizer，避免把合法 credential 教育/排障请求误判为 checkpoint corruption。
敏感 argument key 采用 normalized token semantics 而非有限变体枚举，因此 prefix/suffix/provider 组合中的
token、secret、credential、password、authorization/auth、cookie、signature、private key、access key 均在任意
JSON 深度 fail closed；仅为实际合法计量/可访问性字段保留已测试的 `token_count`、`token_budget`、
`accessibility` 精确例外。
正式 Tool schema 声明且执行后会再次做 owner 校验的 opaque ref 也必须逐字段正向登记；当前仅允许格式受限的
`browser_session_id`，不会把 `session`、cookie 或 bearer token 类字段整体放宽。Pydantic `AnyUrl` 只在此
边界规范化为普通字符串后再经过同一 URL credential/signature 校验，其他任意对象仍拒绝。

## new-turn overwrite contract

同一 conversation `thread_id` 的新 turn 必须生成一份包含**所有 channel**的完整 input：新 request/run，
空 `outputs_by_step`、pending call、observation、errors、Tool trajectory、final response、interrupt；计数器和
stream boundary 全部归零。LangGraph 对既有 checkpoint 的 merge 不能让上个 turn 的 run-scoped facts
泄漏进新 turn。
