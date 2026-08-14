# LangGraph 原生长期记忆节点实施计划

> **执行者必读：** 实施本计划时使用 `superpowers:executing-plans`；新增或修改 pytest、决定验证范围时使用项目 skill `assistant-agent-development-testing`。每个任务按 RED → GREEN → REFACTOR 推进，并在声称完成前使用 `superpowers:verification-before-completion`。

**目标：** 在原生 LangGraph 运行时中，以固定的 `memory_recall`、`publish_response`、`memory_commit` 节点替代 `MemoryPluginHost` 并行运行时，同时支持 LangMem、Mem0 和 disabled 后端。

**架构：** Graph 只依赖 `Node + State + Runtime`。`MemoryNodeBundle` 是只含两个节点、可选 `BaseStore` 和关闭回调的纯装配值对象；`memory_context` 是一次 logical turn 的冻结快照；写入只发生在最终回答进入产品事件流之后，并由最小 ledger 提供去重与结果记录。

**技术栈：** Python、Pydantic、LangGraph `StateGraph` / `Runtime` / `BaseStore`、现有 checkpointer 与 product event 管线、Mem0 HTTP client、可选 LangMem、pytest。

## 实施前置与不可变约束

- 冻结检查点 `1fc9ef19b79c3daa6b74a36c4ef59ce5f01f495f` 已通过 merge commit `ff627eeb700222bf8c4d3c078fd2d8e8f6b78f61` 合入 `integrate/native-langgraph`。实施从该 integration 分支继续；不要改为跟随仍在移动的 feature 分支名。
- 开始实现前使用 `superpowers:using-git-worktrees` 创建隔离工作区。
- 不安装新依赖，除非用户明确允许。LangMem 先以 optional dependency、lazy import 和 fake manager 完成离线实现与测试。
- `MemoryNodeBundle` 只能拥有 `backend_id`、`recall_node`、`commit_node`、`store`、`aclose`；禁止加入 search/save/session/policy/retry/health 等能力。
- assistant/context compiler 的稳定输入只有 recall 后有序、裁剪过的记忆 `text`；ID、score、source、时间只用于 observability。
- ledger 只做 dedup 和 outcome tracking；禁止增加 scheduler、queue、worker、dead-letter 或 session lifecycle。
- Mem0 节点直接调用 client；不得包装为 `BaseStore`。LangMem 节点通过 `runtime.store` 使用 compile 时注入的 Store。
- `publish_response` 是产品事件发布屏障，不是客户端 ACK。commit 失败或超时不能撤回回答，也不能把成功回答改成失败。
- time-travel 矩阵必须原样实现：new invoke recall+commit；resume 复用 snapshot 且只 commit 一次；replay 不 recall/commit；默认 fork 继承 snapshot 且不 commit；refresh fork 重新 recall、默认仍不 commit。
- 默认测试全部使用 mock/local/offline，不调用真实 Provider、Mem0 或 LangMem 服务。

## 文件布局

预计新增：

- `src/assistant_agent/memory/node_bundle.py`
- `src/assistant_agent/memory/backends/disabled.py`
- `src/assistant_agent/memory/backends/mem0.py`
- `src/assistant_agent/memory/backends/langmem.py`
- `src/assistant_agent/memory/commit_ledger.py`
- `tests/tdd/langgraph-memory-nodes/`

预计修改：

- `src/assistant_agent/runtime/assistant_graph_state.py`
- `src/assistant_agent/runtime/assistant_loop_graph.py`
- `src/assistant_agent/runtime/assistant_loop_nodes.py`
- `src/assistant_agent/runtime/assistant_graph_app.py`
- `src/assistant_agent/runtime/graph_runtime.py`
- `src/assistant_agent/runtime/runtime.py`
- `src/assistant_agent/runtime/state.py`
- `src/assistant_agent/context/` 与 tool input memory binding 的实际消费者
- product event projector / publisher 与 Gateway runtime adapter 的实际实现文件
- memory factory/config、`pyproject.toml`
- `tests/core/INVARIANTS.md`
- `tests/core/integration/test_memory_lifecycle.py`
- `tests/core/integration/test_extension_contract.py`
- `docs/memory-service-architecture.md`
- `docs/authority.toml` 及其 contract 指向的相邻当前 authority

最终删除范围必须通过 `rg` 消费者审计确定，候选包括旧 `LongTermMemoryService`、`MemoryPluginHost`、memory session snapshot、ingestion queue/worker 及仅为它们服务的 adapter/CLI。不要删除仍被其他正式能力使用的共享 audit/store，也不要机械删除旧的历史 TDD 目录。

---

### 任务 1：建立版本化 Graph State 记忆契约

**文件：**

- 修改：`src/assistant_agent/runtime/assistant_graph_state.py`
- 新增测试：`tests/tdd/langgraph-memory-nodes/test_memory_state_contract.py`

**步骤：**

1. 写失败测试，覆盖：State schema version 升级；`MemoryContextItem`/`MemoryContext`/`MemoryCommitState`/`ResponsePublishState` 可 JSON round-trip；每次新 product invoke 必须显式覆盖 memory 字段；metadata 不进入 assistant-facing text API。
2. 运行：

   ```bash
   MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langgraph-memory-nodes/test_memory_state_contract.py
   ```

   确认因模型/字段缺失而失败。
3. 实现严格 Pydantic 模型：有界 `items`、稳定 `issue_codes`、不保存任意 metadata/异常/第三方响应；提供只返回有序 `text` 的窄接口或纯函数。
4. 扩展 continuation/node enum 以容纳 `memory_recall`、`publish_response`、`memory_commit`，并在新 invoke 初始化路径显式重置快照、发布和 commit 状态。
5. 重跑单测至通过，执行 `git diff --check`，提交：`feat(memory): add graph memory state contract`。

### 任务 2：增加纯装配 Bundle、disabled 后端和固定 Graph 拓扑

**文件：**

- 新增：`src/assistant_agent/memory/node_bundle.py`
- 新增：`src/assistant_agent/memory/backends/disabled.py`
- 修改：`src/assistant_agent/runtime/assistant_loop_graph.py`
- 修改：`src/assistant_agent/runtime/assistant_graph_app.py`
- 新增测试：`tests/tdd/langgraph-memory-nodes/test_memory_graph_topology.py`

**步骤：**

1. 写失败测试断言 bundle 字段集合精确封闭，disabled recall 返回 empty、commit 返回 skipped，并验证路径：`START → memory_recall → assistant/tool loop → compose_response → publish_response → memory_commit → END`。
2. 增加 frozen dataclass `MemoryNodeBundle`，不实现任何转发或运行时方法；实现无副作用 disabled bundle。
3. Graph builder 接收 bundle，按固定节点名装配；compile 使用 `checkpointer=...` 和 `store=bundle.store`。`AssistantTurnGraphApp` 同时把 bundle 传给 standard/profile/namespaced graph builder；嵌套 profile 必须验证继承父图注入的同一个 `runtime.store`，不得私建第二个 Store。
4. 保留 M5 已建立的稳定 re-entry 门：每个语义节点（包括 recall/publish/commit）都经 `time_travel_anchor → prepare_invocation` 再按 continuation 路由；只有规范化 terminal response 才能进入 publish/commit。
5. 运行该 TDD 文件与已有 graph state/continuation 测试，提交：`feat(memory): compose fixed memory graph nodes`。

### 任务 3：让业务节点只消费冻结的记忆正文

**文件：**

- 修改：`src/assistant_agent/runtime/state.py`
- 修改：`src/assistant_agent/context/` 下实际 context compiler 文件
- 修改：tool input memory binding 的实际实现文件
- 新增测试：`tests/tdd/langgraph-memory-nodes/test_memory_context_consumers.py`

**步骤：**

1. 先用 `rg -n "session_memory_snapshot|frozen_memory_context|context_refs|memory" src/assistant_agent/context src/assistant_agent/runtime src/assistant_agent/tools` 列出真实消费者。
2. 写失败测试证明 context compiler 与 tool binding 只能看到 `tuple[str, ...]` 正文；改变 memory ID、score、source、updated_at 不改变 prompt、工具选择输入或业务路由。
3. 将旧 session snapshot/context refs 入口替换为 Graph State 中 `memory_context` 的有序正文投影；正文始终作为不可信历史上下文，不得拼进 system/developer 权限层。
4. 删除 `AgentState` 中只为旧 freeze/session 服务的字段及兼容分支；若某字段仍有非 memory 消费者，保留并在计划执行记录中说明。
5. 跑新增测试及相关 context/tool 核心测试，提交：`refactor(memory): consume graph snapshot text only`。

### 任务 4：建立回答先发布、记忆后提交的产品事件屏障

**文件：**

- 修改：`src/assistant_agent/runtime/assistant_loop_nodes.py`
- 修改：product event fact/projector/publisher 的实际文件
- 修改：Gateway runtime adapter 的实际文件
- 新增测试：`tests/tdd/langgraph-memory-nodes/test_publish_before_commit.py`

**步骤：**

1. 写一个阻塞型 fake commit node：只有测试释放事件后才结束。断言在 commit 仍阻塞时，产品事件消费者已经收到且只收到一次规范化 final response。
2. 复用现有 `RunFinalProductFact` 和 `ProductEventProjector`，不新增平行发布事件类型。State 保存稳定 `final_fact_id`，保证节点重入复用同一 occurrence identity；`publish_response` 只通过 `runtime.context.product_fact_writer` 发布并写最小状态，不持有 socket/HTTP 对象。
3. 为 async Graph 路径补齐 `GraphRuntimeContext.product_fact_writer`（sync 路径已注入）；Gateway adapter 将 `final_response` 纳入运行中转发，并记录 `final_response_seen`。runtime 返回结果时若已看到同一 final fact，不再合成第二条 final。token delta 行为保持不变。
4. 分离 `published` 与 Gateway 的 delivered/ACK 观测：Graph 只等待 publish 调用成功，不等待客户端 ACK。
5. 增加 commit failed/timed_out 测试，确认回答仍成功、Graph 到 END、只更新 memory outcome。跑 Gateway 多轮/stream/final 相关核心测试，提交：`feat(memory): publish response before memory commit`。

### 任务 5：实现最小 commit ledger 与 Mem0 直接节点

**文件：**

- 新增：`src/assistant_agent/memory/commit_ledger.py`
- 新增：`src/assistant_agent/memory/backends/mem0.py`
- 修改：memory config/factory 与现有 Mem0 client 的实际文件
- 新增测试：`tests/tdd/langgraph-memory-nodes/test_mem0_nodes.py`
- 新增测试：`tests/tdd/langgraph-memory-nodes/test_memory_commit_ledger.py`

**步骤：**

1. 参考现有 `ToolOperationStore` 的事务 reserve/outcome 模式，但不要把 Memory 伪装成 Tool、不要复用 `ToolOperationRequest/tool_name/profile` 业务模型。实现独立、极薄的 `MemoryCommitLedger` protocol 与 SQLite `memory_commit_events` 表；它可以和现有本地 operation DB 共用文件，但必须使用独立表和 memory 字段语义。不新增队列、worker 或后台 retry。
2. 写失败测试覆盖稳定 `memory_event_id`：同一 logical turn 的 invoke/resume 相同，不同 backend/input schema 不同；succeeded 去重；invoking 中断映射 outcome_unknown；原始异常和第三方响应不进 State。
3. Mem0 recall node 直接调用现有 client search 并规范化/排序/裁剪；commit node 从受信 runtime identity 和规范化本轮对话构造输入，先 ledger reserve，再调用 client，再记录 outcome。
4. 现有 Mem0 client 是同步接口，首版 node 也保持同步，以兼容 `AssistantTurnGraphApp.invoke()` 与 async graph 两条入口；沿用 client 自身显式 timeout，不自动 retry。继续使用既有 opaque identity binding，不从用户文本构造 namespace。
5. 覆盖 degraded recall、empty recall、commit timeout/failed、duplicate、close 回调；全程 fake client/offline。提交：`feat(memory): add direct mem0 graph nodes`。

### 任务 6：实现 invoke、resume、replay、fork 的冻结快照语义

**文件：**

- 修改：`src/assistant_agent/runtime/assistant_loop_graph.py`
- 修改：`src/assistant_agent/runtime/runtime.py`
- 修改：Graph invoke/fork request model 的实际文件
- 新增测试：`tests/tdd/langgraph-memory-nodes/test_memory_time_travel.py`

**步骤：**

1. 参数化写出五行批准矩阵，记录 recall/commit 调用次数、snapshot_id 和 input logical origin。
2. 新 invoke 从 `memory_recall` 开始；resume 从 checkpoint continuation 继续并复用 context；只允许原 product turn 首次 terminal commit。
3. replay 和默认 fork 继承所选 checkpoint 快照并跳过 recall/commit；缺少有效 memory context 时精确模式 fail closed。
4. 为受信 fork 请求增加显式 `refresh_memory`；refresh fork 重新 recall，标记非精确历史重放，但默认仍跳过 commit。
5. 删除 runtime 在 Graph 外调用旧 `LongTermMemoryService.prepare/attach/finalize` 的路径。运行 time-travel、interrupt/resume、checkpoint 核心测试，提交：`feat(memory): enforce frozen snapshot time travel semantics`。

### 任务 7：增加 LangMem 可选原生后端

**文件：**

- 修改：`pyproject.toml`
- 新增：`src/assistant_agent/memory/backends/langmem.py`
- 修改：memory config/factory
- 新增测试：`tests/tdd/langgraph-memory-nodes/test_langmem_nodes.py`

**步骤：**

1. 在 optional dependency group 中声明兼容版本范围，例如 `memory-langmem = ["langmem>=0.0.30,<0.1"]`；默认安装不得导入失败。
2. 用 lazy import 构造 LangMem manager/store。明确配置 LangMem 但包缺失时返回可解释配置错误并 fail closed，不静默降级 Mem0/disabled。
3. fake `BaseStore`/manager 测试断言 compile 注入的对象可由节点从 `runtime.store` 取得；禁止把 Store 放入 State，禁止使用闭包里的另一个 Store 实例。
4. recall 规范化为同一 `MemoryContext`，commit 经过同一最小 ledger；LangMem extraction/update/merge 语义留在 manager 内。
5. 未获得用户安装许可时只运行 fake/offline 测试；不要执行 pip install 或真实 LangMem 集成。提交：`feat(memory): add optional native langmem backend`。

### 任务 8：切换 composition root 并删除旧 Memory Runtime 主线

**文件：**

- 修改：application composition root、runtime factory、config 与 shutdown 的实际文件
- 删除：消费者审计确认仅服务旧 runtime 的 Host/service/session/queue/worker 文件
- 修改：`tests/core/INVARIANTS.md`
- 修改：`tests/core/integration/test_memory_lifecycle.py`
- 修改：`tests/core/integration/test_extension_contract.py`

**步骤：**

1. factory 根据受信配置只构造一个 active bundle；显式错误 fail closed；应用关闭时调用可选 `aclose`，Store setup/migration/close 仍由 composition root 负责。
2. 用 `rg` 审计 `MemoryPluginHost|LongTermMemoryService|open_session|ingestion|session_memory_snapshot` 的所有 source/test/script/docs 消费者，逐项迁移后再删除。不得保留两条同时工作的 runtime 主线。
3. 在 `tests/core/INVARIANTS.md` 注册 `MEMORY-001`：长期记忆只通过固定 Graph 节点读写，快照冻结，回答先发布，派生历史执行不产生写入。
4. 将现有 core memory lifecycle 测试改为后端无关 Graph 不变量；从 `EXT-001` probe 删除旧 `MemoryPluginHost` 能力，但保留仍有效的薄 extension/composition 契约。
5. 运行新增 TDD 目录和受影响 core integration tests，提交：`refactor(memory): remove parallel memory runtime`。

### 任务 9：同步当前 authority 并执行分层验证

**文件：**

- 修改：`docs/memory-service-architecture.md`
- 修改：`docs/authority.toml`
- 按 contract 边界复核并按需修改相邻 `docs/*.md`、`tests/README.md`、`evals/README.md`

**步骤：**

1. 将已实施事实写回 memory authority：Node/State/Runtime/optional Store 边界、纯 bundle、正文稳定契约、publish barrier、最小 ledger、五类 time-travel 语义；删除 Host/session/queue 已失效描述。
2. 按 `docs/authority.toml` 的 `read_when`/`source_globs` 复核 runtime、Gateway、context、durability、observability owner。只修改真实跨 contract 的 authority，不机械制造 diff。
3. 执行静态与残留检查：

   ```bash
   rg -n "MemoryPluginHost|LongTermMemoryService|session_memory_snapshot|frozen_memory_context" src tests/core README.md docs scripts AGENTS.md
   git diff --check
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pip check
   ```

   对每个残留给出保留原因；预期删除的运行时引用不得静默存在。
4. 执行专项和核心测试：

   ```bash
   MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langgraph-memory-nodes
   MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/integration/test_memory_lifecycle.py tests/core/integration/test_extension_contract.py
   MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core
   ```

5. 因修改当前 authority，运行：

   ```bash
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
   ```

6. 使用 `git status --short` 和 `git diff --stat` 确认只包含本任务文件，提交：`docs(memory): document native graph memory architecture`。
7. 最终报告按 `tests/README.md` 写明：`Core invariant: MEMORY-001`、运行过的 `Tests:`、未安装/未真实调用 LangMem/Mem0 的限制，以及下一步是否需要用户授权安装 optional dependency 做真实集成验证。

## Code Review 必查项

- `MemoryNodeBundle` 是否仍是无行为的 composition object。
- assistant/tool/context 是否只依赖有序 memory text，不依赖后端 metadata。
- commit ledger 是否只做去重/结果记录，是否出现任何后台投递基础设施。
- final response 是否在 commit 开始前已进入产品流，是否可能重复发布。
- Graph checkpoint 恢复后是否复用同一 snapshot，五类 time-travel 是否严格匹配矩阵。
- Mem0 是否被错误包装为 Store；LangMem Store 是否由 compile 注入并从 `runtime.store` 读取。
- 旧 Host/service/session/queue 是否真正退出运行主线，而非仅改名或被新 factory 包裹。
- 所有真实 backend 调用是否保持显式配置、受信 identity、超时、脱敏和 mock/offline 测试边界。
