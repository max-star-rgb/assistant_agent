# 核心不变量登记

下表是核心框架测试的稳定不变量登记。每个条目定义可观察的结构化契约，并指定负责守住该契约的
`tests/core/...` 测试文件。

| ID | 结构化契约 | 负责文件 |
| --- | --- | --- |
| POLICY-001 | 默认 pytest 收集范围仅为 `tests/core`；核心测试文件已登记，且不以完整人类文案或 feature implementation import 绑定行为。 | `tests/core/unit/test_test_policy.py` |
| BOOT-001 | mock/real provider mode 显式分离；离线启动不读取真实 Provider 配置且不会发起网络调用。 | `tests/core/integration/test_runtime_lifecycle.py` |
| RUN-001 | 生产 run 直接返回标准 messages；thread/run/checkpoint/interrupt/resume/cancel 与终态由 LangGraph Agent Server 所有，Graph state 不保存平行产品终态。 | `tests/core/integration/test_runtime_lifecycle.py` |
| LOOP-001 | 进程内复用唯一静态父 `AssistantRootGraph`；父图以单个 `memory_recall` 作为每 run 回答前 checkpoint，再执行结构化 fast/planning/coding 路由与回答后的 Memory debounce refresh。fast、planning 与 coding 不设置 model 或单 Tool run 累计次数上限；同一 model superstep 内同名 Tool 最多并行调用 12 个实例，后续 turn 可再次调用任意参数，并在 LangGraph `recursion_limit` 耗尽前使用剩余 step 生成无 Tool 的自然综合答复。coding 直接装配官方 `create_deep_agent` 编译出的 `AssistantCodingAgent`，使用其原生 Todo、filesystem、subagent、summarization、HITL 与 `ToolNode` 循环。项目不实现第二套 coding StateGraph、patch/review/repair/validation/integration loop；只通过一个 `SandboxBackendProtocol` adapter 把官方文件与命令 Tool 绑定到认证 identity、thread 和进程内固定当前项目对应的隔离 Git worktree。 | `tests/core/integration/test_runtime_lifecycle.py` |
| TOOL-001 | 生产进程内内建 Tool 均由官方 `@tool` factory 创建为标准 `BaseTool` 并经 `ToolNode` 执行；官方 `ToolRuntime` 注入直接隐藏模型 schema 中的 runtime-owned 参数，不存在动态生成的项目 schema 层，`ToolRuntime.server_info.user.identity` 注入受信身份；成功结果为标准 `ToolMessage(content, artifact)`，非 read 预期失败与 read 重试耗尽均成为有界 error `ToolMessage`，不终止生产 Graph。 | `tests/core/contract/test_tool_contract.py` |
| EXT-001 | 生产本地业务 Tool 由受信静态清单装配；MCP 使用官方 adapter、显式 allowlist 与确定性 namespace，二者都输出标准 `BaseTool`。业务 inventory 不重复实现或暴露文件、执行、Skill loader Tool；仓库文件能力只由 Agent 内的 Deep Agents `FilesystemMiddleware` 注入。 | `tests/core/contract/test_extension_contract.py` |
| MEMORY-001 | `enable_memory` 默认为 true；启用时每个顶层 chat run 在回答前通过单个 `memory_recall` recall 一次并冻结 `memory_context`，回答后通过官方 Agent Server SDK 在由 chat thread 确定性派生的 companion Memory thread 上 rollback 旧 pending Memory run，并立即 enqueue 新 delayed Memory run。Assistant 或单次 run 将其设为 false 时，recall 冻结为空且不调度 extraction。chat thread 不保留后台 pending run，extract 不在 chat run 内执行；recall、refresh 与独立 extraction 均使用原生 `RetryPolicy` 最多尝试三次，耗尽后对应 run 明确进入 error 而非静默 success。 | `tests/core/integration/test_memory_lifecycle.py` |
| CTX-001 | 生产上下文使用标准 messages；fast/planning 的 system prompt 由统一分层构建器组装，Studio Assistant 指令、用户日期/地区和冻结 Memory 按现有边界投影。正常 model call 预算耗尽后额外预留一次显式 `tool_choice="none"` 的无 Tool 真实模型调用，复用已有 messages 与 Tool observation 自然收尾，不注入人工限额 `AIMessage`。task 子 Agent 只获得 description、冻结 Memory 和 execution mode，父 conversation、Todo、Tool Profile 与内部 transcript 不回灌。Skills、Tool Profile、Todo、task、summarization、retry 与 HITL 均由官方 middleware 所有；未激活 Profile 的 Tool 即使被模型从历史中生成，抵达执行边界时也只返回可恢复 error `ToolMessage`，不得进入 Tool handler。fast 自动放行业务 Tool，planning task 内非 read Tool 触发原生 HITL；coding 使用 Deep Agents 官方 state 与 HITL，工作区身份和宿主路径只在 backend 调用时解析，不进入模型可见 context。 | `tests/core/integration/test_context_lifecycle.py`；`tests/core/integration/test_runtime_lifecycle.py` |
| GATE-001 | Agent Server 原生拥有生产 thread、run、queue、checkpoint、cancel 与 stream 生命周期；生产 graph ID 为 `assistant-native-v3`，不注册 v1/v2 alias，也不注册 coding inspector 或独立 coding run graph；Agent Server auth 在 graph-aware thread create、显式 metadata identity update 与 chat run create 边界强制 owner + v3 identity，独立 Memory/worker 保留自身 identity，旧 run 的 interrupt/rollback 仍可按 owner drain/cancel，SDK adapter 再复核；v1/v2/unknown thread 与 legacy checkpoint 只读，不能进入 v3 run/resume/replay/stream；`/agent-service/v1` 只做媒体 wire 解析、原生资源关联和响应投影，不拥有平行 Graph Runtime。 | `tests/core/contract/test_gateway_contract.py` |
| IDENT-001 | Agent Server `user.identity` 是生产运行的唯一用户身份；tokenless auth hook 在 mock/real 模式下都从客户端 `X-Assistant-User` 构造 identity，缺省为 `local-developer`；Studio 可编辑的 Assistant Runtime Context 只包含 execution preset 和 `enable_memory` 偏好，不复制身份、prompt、仓库选择、入口、媒体能力、实时模式或视觉凭据；服务端入口事实与视觉 capability 只经 namespaced run metadata 注入，媒体可用性由当前标准消息投影判定；assistant、graph、thread、run、媒体 connection 与 delivery ID 相互分离，同一 conversation 的 `thread_id` 在后续 run 间稳定。 | `tests/core/contract/test_gateway_contract.py`；`tests/core/integration/test_runtime_lifecycle.py`；`tests/core/integration/test_durable_lifecycle.py` |
| DUR-001 | Durable schedule、resume、cancel、background queue 与 failure atomicity 依照通用状态机转换；同一 store 上重建 service/worker 后，已登记 schedule 按时恢复且仅执行一次。 | `tests/core/integration/test_durable_lifecycle.py` |
| OBS-001 | 生产 compiled graph 通过 LangChain/LangGraph callback 与 LangSmith native tracing 暴露实际 Graph/Node/LLM/Tool 树；production composition 不重建 canonical 或 OTel shadow tree。 | `tests/core/contract/test_observability_contract.py` |

LOOP-001 后台 delegation 补充约束：生产 composition 在同步 planning `task` 继续复用同一个 fast agent 的同时，还以同一
模型、prompt 与 middleware 配置构造独立只读 `AssistantBackgroundWorker`，并注册为 `assistant-worker-v1`。fast/planning
均静态注册 Deep Agents `AsyncSubAgentMiddleware` 的五个 task lifecycle Tool，并通过独立 Profile 渐进暴露；worker 不注册这些 Tool，不能递归 delegation。

CTX-001 后台 delegation 补充约束：父图、fast 与 planning 共享按 task ID 合并的 `async_tasks` handle channel，使模式切换
不丢失 task/thread/run identity；同步 task worker 只可回传新 handle，worker transcript、Memory、Todo、Skill/Profile state
不复制到父会话。

GATE-001 后台 delegation 补充约束：除 `assistant-native-v3` 与 `assistant-memory-v1` 外，Agent Server 还注册
`assistant-worker-v1`；每个后台任务使用独立 thread/run，并在 thread/run create 边界按 worker graph ID 强制同一 owner。
