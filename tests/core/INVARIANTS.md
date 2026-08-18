# 核心不变量登记

下表是核心框架测试的稳定不变量登记。每个条目定义可观察的结构化契约，并指定负责守住该契约的
`tests/core/...` 测试文件。

| ID | 结构化契约 | 负责文件 |
| --- | --- | --- |
| POLICY-001 | 默认 pytest 收集范围仅为 `tests/core`；核心测试文件已登记，且不以完整人类文案或 feature implementation import 绑定行为。 | `tests/core/unit/test_test_policy.py` |
| BOOT-001 | mock/real provider mode 显式分离；离线启动不读取真实 Provider 配置且不会发起网络调用。 | `tests/core/integration/test_runtime_lifecycle.py` |
| RUN-001 | 生产 run 直接返回标准 messages；thread/run/checkpoint/interrupt/resume/cancel 与终态由 LangGraph Agent Server 所有，Graph state 不保存平行产品终态。 | `tests/core/integration/test_runtime_lifecycle.py` |
| LOOP-001 | 进程内复用唯一静态父 `AssistantRootGraph`；父图直接执行每 run recall、结构化 fast/planning 路由与回答后的 Memory debounce refresh；fast 是 `create_agent`，planning 是显式 StateGraph 且所有 worker 复用同一个 fast graph。 | `tests/core/integration/test_runtime_lifecycle.py` |
| TOOL-001 | 生产进程内内建 Tool 均由官方 `@tool` factory 创建为标准 `BaseTool` 并经 `ToolNode` 执行；官方 `ToolRuntime` 注入直接隐藏模型 schema 中的 runtime-owned 参数，不存在动态生成的项目 schema 层，`ToolRuntime.server_info.user.identity` 注入受信身份，结果为标准 `ToolMessage(content, artifact)`。 | `tests/core/contract/test_tool_contract.py` |
| EXT-001 | 生产本地 Tool 由受信静态清单装配；MCP 使用官方 adapter、显式 allowlist 与确定性 namespace，二者都输出标准 `BaseTool`。 | `tests/core/contract/test_extension_contract.py` |
| MEMORY-001 | 每个顶层 chat run 在回答前 recall 一次并冻结 `memory_context`；回答后通过官方 Agent Server SDK rollback 同 thread 旧的 pending Memory run并立即 enqueue 新的 delayed Memory run，pending chat run 不受影响，extract 不在 chat run 内执行。 | `tests/core/integration/test_memory_lifecycle.py` |
| CTX-001 | 生产上下文使用标准 messages；冻结 Memory 仅作为最新真实用户请求前的临时 `HumanMessage` 进入单次模型请求，不写入对话 state；模型调用使用官方 limit/summarization/HITL middleware，fast 自动放行，planning 对非 read Tool 触发 HITL。 | `tests/core/integration/test_context_lifecycle.py` |
| GATE-001 | Agent Server 原生拥有生产 thread、run、queue、checkpoint、cancel 与 stream 生命周期；`/agent-service/v1` 只做媒体 wire 解析、原生资源关联和响应投影，不拥有平行 Graph Runtime。 | `tests/core/contract/test_gateway_contract.py` |
| IDENT-001 | Agent Server `user.identity` 是生产运行的唯一用户身份；tokenless auth hook 在 mock/real 模式下都从客户端 `X-Assistant-User` 构造 identity，缺省为 `local-developer`；Assistant Runtime Context 只携带非身份入口能力与实时媒体模式，不复制用户或租户字段；assistant、graph、thread、run、媒体 connection 与 delivery ID 相互分离，同一 conversation 的 `thread_id` 在后续 run 间稳定。 | `tests/core/contract/test_gateway_contract.py`；`tests/core/integration/test_runtime_lifecycle.py`；`tests/core/integration/test_durable_lifecycle.py` |
| DUR-001 | Durable schedule、resume、cancel、background queue 与 failure atomicity 依照通用状态机转换；同一 store 上重建 service/worker 后，已登记 schedule 按时恢复且仅执行一次。 | `tests/core/integration/test_durable_lifecycle.py` |
| OBS-001 | 生产 compiled graph 通过 LangChain/LangGraph callback 与 LangSmith native tracing 暴露实际 Graph/Node/LLM/Tool 树；production composition 不重建 canonical 或 OTel shadow tree。 | `tests/core/contract/test_observability_contract.py` |
