# 核心不变量登记

下表是核心框架测试的稳定不变量登记。每个条目定义可观察的结构化契约，并指定负责守住该契约的
`tests/core/...` 测试文件。

| ID | 结构化契约 | 负责文件 |
| --- | --- | --- |
| POLICY-001 | 默认 pytest 收集范围仅为 `tests/core`；核心测试文件已登记，且不以完整人类文案或 feature implementation import 绑定行为。 | `tests/core/unit/test_test_policy.py` |
| BOOT-001 | mock/real provider mode 显式分离；离线启动不读取真实 Provider 配置且不会发起网络调用。 | `tests/core/integration/test_runtime_lifecycle.py` |
| RUN-001 | Run 的同步兼容入口与原生异步 graph 入口产生同义的产品终态；内部显式启用的 graph interrupt 产生 `waiting_user` 可恢复非终态，不写 terminal lifecycle，resume 后才以 completed、failed 或 cancelled 之一唯一终结；终态后不再产生可执行状态转换，并对已注册 Tool 的可选 terminal lifecycle hook 做一次 best-effort 通知。 | `tests/core/integration/test_runtime_lifecycle.py` |
| LOOP-001 | 每个 Runtime 稳定持有一个 compiled `AssistantTurnGraph`；通用 assistant loop 按 assistant / governed tool / await input / compose 的原生条件边推进。Graph family 使用版本化、checkpoint-safe state 与 standard/planner/worker/verifier profile child；同一 thread 恢复后的 scripted Provider/Tool trajectory 与不中断执行保持等价。 | `tests/core/integration/test_runtime_lifecycle.py` |
| TOOL-001 | 每次本地显式 tool call 都经过 validation、ToolExecutor、Registry contract lookup、状态生命周期与结构化结果治理；默认 execution backend 调用 Registry Tool，受信 composition root 可在 ToolExecutor 内注入无副作用 backend，但不能绕过上述治理。可恢复的 write/dangerous 调用还必须先经过稳定 operation identity 与持久 operation barrier；已进入或已提交的同一 operation 不得重复触发 backend 副作用。 | `tests/core/contract/test_tool_contract.py`；`tests/core/integration/test_runtime_lifecycle.py` |
| EXT-001 | Probe Tool 与受信任 capability Plugin 通过声明的 identity、版本、schema 和显式装配接入；扩展不能绕过 Tool 治理链。 | `tests/core/contract/test_extension_contract.py` |
| MEMORY-001 | 长期记忆只通过固定的 `memory_recall` / `memory_commit` Graph 节点读写；`memory_context` 是 logical turn 的 checkpoint 冻结快照；最终回答先发布；replay/fork 不产生长期记忆写入。 | `tests/core/integration/test_memory_lifecycle.py` |
| CTX-001 | Context budget、compaction 与因果配对保持可验证的结构化计数和事件关系。 | `tests/core/integration/test_context_lifecycle.py` |
| GATE-001 | Agent Server 原生拥有生产 thread、run、queue、checkpoint、cancel 与 stream 生命周期；`/agent-service/v1` 只做媒体 wire 解析、原生资源关联和响应投影，不拥有平行 Graph Runtime。 | `tests/core/contract/test_gateway_contract.py` |
| IDENT-001 | Agent Server auth principal、user/tenant context、thread、run、媒体 connection 与 delivery ID 相互分离；同一 conversation 的 `thread_id` 在后续 run 间稳定，不得以 `run_id` 或连接 ID 代替 thread。 | `tests/core/contract/test_gateway_contract.py`；`tests/core/integration/test_runtime_lifecycle.py`；`tests/core/integration/test_durable_lifecycle.py` |
| DUR-001 | Durable schedule、resume、cancel、background queue 与 failure atomicity 依照通用状态机转换；同一 store 上重建 service/worker 后，已登记 schedule 按时恢复且仅执行一次。 | `tests/core/integration/test_durable_lifecycle.py` |
| OBS-001 | canonical event、trace correlation、span timing与持久化 read-through 在结构化观测输出中保持稳定关联；产品事件只单向投影已发生的 native graph/runtime facts，不模拟 graph node lifecycle；server canonical store 不为 LangSmith 重建 OTel graph tree。 | `tests/core/integration/test_runtime_lifecycle.py`；`tests/core/contract/test_observability_contract.py` |
