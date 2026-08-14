# 核心不变量登记

下表是核心框架测试的稳定不变量登记。每个条目定义可观察的结构化契约，并指定负责守住该契约的
`tests/core/...` 测试文件。

| ID | 结构化契约 | 负责文件 |
| --- | --- | --- |
| POLICY-001 | 默认 pytest 收集范围仅为 `tests/core`；核心测试文件已登记，且不以完整人类文案或 feature implementation import 绑定行为。 | `tests/core/unit/test_test_policy.py` |
| BOOT-001 | mock/real provider mode 显式分离；离线启动不读取真实 Provider 配置且不会发起网络调用。 | `tests/core/integration/test_runtime_lifecycle.py` |
| RUN-001 | 生产 run 直接返回标准 messages；thread/run/checkpoint/interrupt/resume/cancel 与终态由 LangGraph Agent Server 所有，Graph state 不保存平行产品终态。 | `tests/core/integration/test_runtime_lifecycle.py` |
| LOOP-001 | 唯一父 `AssistantRootGraph` 在一次 Memory recall 后按结构化 fast/planning 路由；fast 是 `create_agent`，planning 是显式 StateGraph 且所有 worker 复用同一个 fast graph，汇流后只 commit 一次。 | `tests/core/integration/test_runtime_lifecycle.py` |
| TOOL-001 | 生产本地 Tool 使用标准 `BaseTool`/`ToolNode`；模型可见 schema 隐藏 runtime-owned 参数，`ToolRuntime` 注入受信身份，结果为标准 `ToolMessage(content, artifact)`。 | `tests/core/contract/test_tool_contract.py` |
| EXT-001 | 生产本地 Tool 由受信静态清单装配；MCP 使用官方 adapter、显式 allowlist 与确定性 namespace，二者都输出标准 `BaseTool`。 | `tests/core/contract/test_extension_contract.py` |
| MEMORY-001 | 长期记忆只在父图固定 `memory_recall` / `memory_commit` 节点读写；两种执行模式每 run 各一次，worker 只读冻结 `memory_context`。 | `tests/core/integration/test_memory_lifecycle.py` |
| CTX-001 | 生产上下文使用标准 messages、带明确 untrusted/frozen 标记的 Memory dynamic prompt，以及官方 limit/summarization/HITL middleware。 | `tests/core/integration/test_context_lifecycle.py` |
| GATE-001 | Agent Server 原生拥有生产 thread、run、queue、checkpoint、cancel 与 stream 生命周期；`/agent-service/v1` 只做媒体 wire 解析、原生资源关联和响应投影，不拥有平行 Graph Runtime。 | `tests/core/contract/test_gateway_contract.py` |
| IDENT-001 | Agent Server auth principal、user/tenant context、thread、run、媒体 connection 与 delivery ID 相互分离；同一 conversation 的 `thread_id` 在后续 run 间稳定，不得以 `run_id` 或连接 ID 代替 thread。 | `tests/core/contract/test_gateway_contract.py`；`tests/core/integration/test_runtime_lifecycle.py`；`tests/core/integration/test_durable_lifecycle.py` |
| DUR-001 | Durable schedule、resume、cancel、background queue 与 failure atomicity 依照通用状态机转换；同一 store 上重建 service/worker 后，已登记 schedule 按时恢复且仅执行一次。 | `tests/core/integration/test_durable_lifecycle.py` |
| OBS-001 | 生产 compiled graph 通过 LangChain/LangGraph callback 与 LangSmith native tracing 暴露实际 Graph/Node/LLM/Tool 树；production composition 不重建 canonical 或 OTel shadow tree。 | `tests/core/contract/test_observability_contract.py` |
