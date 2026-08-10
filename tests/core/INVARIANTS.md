# 核心不变量登记

下表是核心框架测试的稳定不变量登记。每个条目定义可观察的结构化契约，并指定负责守住该契约的
`tests/core/...` 测试文件。

| ID | 结构化契约 | 负责文件 |
| --- | --- | --- |
| POLICY-001 | 默认 pytest 收集范围仅为 `tests/core`；核心测试文件已登记，且不以完整人类文案或 feature implementation import 绑定行为。 | `tests/core/unit/test_test_policy.py` |
| BOOT-001 | mock/real provider mode 显式分离；离线启动不读取真实 Provider 配置且不会发起网络调用。 | `tests/core/integration/test_runtime_lifecycle.py` |
| RUN-001 | Run 只能以 completed、failed 或 cancelled 之一终态结束；终态后不再产生可执行状态转换，并对已注册 Tool 的可选 terminal lifecycle hook 做一次 best-effort 通知。 | `tests/core/integration/test_runtime_lifecycle.py` |
| LOOP-001 | 通用 assistant loop 按事件和 tool call 结果推进，直到确定终态或可解释失败。 | `tests/core/integration/test_runtime_lifecycle.py` |
| TOOL-001 | 每次本地显式 tool call 都经过 validation、execution、registry 治理链，并产生结构化结果。 | `tests/core/contract/test_tool_contract.py`；`tests/core/integration/test_runtime_lifecycle.py` |
| EXT-001 | Probe Tool 与受信任 capability Plugin 通过声明的 identity、版本、schema、显式装配和宿主治理契约接入；扩展不能绕过其所属治理链。 | `tests/core/contract/test_extension_contract.py` |
| CTX-001 | Context budget、compaction 与因果配对保持可验证的结构化计数和事件关系。 | `tests/core/integration/test_context_lifecycle.py` |
| GATE-001 | Gateway 的 session、run、turn 与 frame 按定义的生命周期创建、转移、终止和重连。 | `tests/core/contract/test_gateway_contract.py` |
| IDENT-001 | session/run 与 durable subscription 按 user/agent 边界隔离；入口身份字段原样保留并用于关联。 | `tests/core/integration/test_runtime_lifecycle.py`；`tests/core/integration/test_durable_lifecycle.py` |
| DUR-001 | Durable schedule、resume、cancel、background queue 与 failure atomicity 依照通用状态机转换；同一 store 上重建 service/worker 后，已登记 schedule 按时恢复且仅执行一次。 | `tests/core/integration/test_durable_lifecycle.py`；`tests/core/integration/test_memory_lifecycle.py` |
| OBS-001 | canonical event、trace correlation、span timing、持久化 read-through 与后台生命周期在结构化观测输出中保持稳定关联。 | `tests/core/integration/test_runtime_lifecycle.py`；`tests/core/integration/test_memory_lifecycle.py`；`tests/core/contract/test_observability_contract.py` |
