# LangGraph 原生长期记忆节点设计

日期：2026-08-13
状态：已批准，待实施

## 1. 背景与定位

项目即将原生使用 LangGraph `StateGraph`、checkpointer、`Runtime`、interrupt、resume 与 time travel。
当前长期记忆仍由 `LongTermMemoryService -> MemoryPluginHost -> MemoryPlugin` 维护另一套 session、freeze、
ingestion queue 和四阶段生命周期。这套抽象能够治理第三方实现，但也形成了与 Graph 并行的 Memory Runtime，
使 LangMem 的原生 `BaseStore` 集成和 Graph 的执行位置不能自然结合。

本设计将长期记忆在一次 Assistant turn 中的位置收归 LangGraph，以固定的 `memory_recall`、
`memory_commit` 节点表达读取和写入时机。不同记忆产品不再实现统一 Memory SDK，而是在 composition root
装配为原生节点。Graph 只依赖标准 State，不识别 LangMem、Mem0 或其他后端。

本文件是开发设计材料，不是当前 authority。只有代码、测试和当前文档完成迁移后，
`docs/memory-service-architecture.md` 才能改为新的事实权威。本设计实施完成后替代
`2026-08-07-assistant-memory-plugin-api-design.md` 中复杂 `MemoryPluginHost` 运行时方向。

## 2. 目标

1. 以 LangGraph `Node + State` 作为长期记忆在执行流程中的统一抽象。
2. 删除复杂的 `MemoryInterface`、`MemoryPluginHost`、session freeze 和后台 ingestion Runtime。
3. 同时支持原生使用 `BaseStore` 的 LangMem、直接调用 SDK/API 的 Mem0，以及后续第三方后端。
4. 让 assistant、tool 等业务节点只消费标准化 `memory_context`，不能直接访问长期记忆后端。
5. 在最终答案进入产品交付流后直接执行 `memory_commit`，且记忆失败不改变已经生成的回答。
6. 保留外部副作用必需的最小幂等 ledger，而不再建设一套通用 Memory Runtime。
7. 明确 invoke、resume、replay 和 fork 的召回与写入语义。

## 3. 非目标

- 不把 Mem0 包装成 LangGraph `BaseStore`。
- 不为所有第三方 Memory Engine 设计统一 CRUD、session 或 capability SDK。
- 不把 WebSocket、HTTP ACK、重连或 Gateway delivery 生命周期放入 Graph State。
- 不让 assistant/tool 节点获得长期记忆写权限。
- 不以 LangGraph checkpoint 代替外部记忆 API 的幂等保证。
- 不在本次迁移中提供模型可自由调用的 `memory_search`、`memory_save` Tool。
- 不在本次迁移中定义多后端同时召回、融合或双写策略；一个 compiled Assistant graph 只装配一个 active backend。
- 不自动删除长期记忆。session reset、thread 删除与长期记忆删除是不同操作。
- 不在本设计中扩展新的多模态长期记忆协议；已有受管媒体能力必须在迁移时逐项确认真实消费者后再决定保留或删除。

## 4. 核心原则

### 4.1 Graph 统一流程，不统一第三方 SDK

Graph 的稳定契约是节点名称、边和 State 字段。后端只需贡献可调用节点，不需要伪装成同一种存储产品：

```text
Application Composition Root
│
├── Checkpointer
├── MemoryBackendFactory
│    ├── LangMemBackend
│    │    ├── recall_node
│    │    ├── commit_node
│    │    └── optional BaseStore
│    ├── Mem0Backend
│    │    ├── recall_node
│    │    └── commit_node
│    └── OtherBackend
│         ├── recall_node
│         └── commit_node
│
└── Assistant StateGraph
```

### 4.2 State 是本轮快照，Store 是跨 thread 资源

`BaseStore` 与 Graph State 是两个独立平面：

- checkpointer 按 thread 和 graph step 保存 State；
- `BaseStore` 保存跨 thread 的长期数据；
- `graph.compile(store=store)` 只把 Store 注入 `runtime.store`；
- Store 对象和 Store 全量内容不会自动进入 State；
- 只有 `memory_recall` 显式返回的标准化结果会进入 `state.memory_context`。

因此，`memory_context` 表示“本逻辑 turn 在 recall 时刻获得的有界、prompt-safe 记忆快照”，不是长期
数据库的镜像。它会进入 checkpoint，以保证 assistant/tool loop 和 resume 消费同一份结果。

### 4.3 写权限只有一个入口

长期记忆写 authority 只属于 `memory_commit`。assistant、tool、context builder、Gateway 和入口适配器
不得直接调用 Memory backend。未来若确实需要用户显式记忆命令，应新增受治理的 `memory_command`
节点并单独设计授权与幂等语义，不能旁路 `memory_commit`。

## 5. 目标 Graph

```text
START
  ↓
memory_recall
  ↓
assistant ←───────────┐
  │                   │
  ├── execute_tool ───┘
  ├── await_input → interrupt
  └── compose_response
            ↓
      publish_response
            ↓
       memory_commit
            ↓
           END
```

`publish_response` 表示最终回答已写入 Runtime/Gateway 的产品交付流，用户侧可以收到；它不表示客户端
ACK。Gateway 继续负责 socket/HTTP 写入、重连、outbox 和真实 delivery 生命周期。Graph 只通过已有的
产品事件边界发布回答，不能直接持有连接对象。

顺序不变量如下：

1. `compose_response` 形成规范化最终回答；
2. `publish_response` 成功后，才允许进入 `memory_commit`；
3. `memory_commit` 直接调用 LangMem/Mem0；
4. commit 成功、失败或超时后 Graph 才到达 `END`；
5. commit 失败只影响记忆观测，不撤回回答，也不把成功回答改为失败。

回答 token delta 可以在 assistant loop 中提前流式发布。这里约束的是规范化最终回答的发布屏障。

## 6. 最小装配契约

项目最多保留一个薄的装配值对象：

```python
@dataclass(frozen=True)
class MemoryNodeBundle:
    backend_id: str
    recall_node: Callable[..., Awaitable[dict[str, object]]]
    commit_node: Callable[..., Awaitable[dict[str, object]]]
    store: BaseStore | None = None
    aclose: Callable[[], Awaitable[None]] | None = None
```

该类型只负责 composition，不提供 recall/commit 转发方法，不保存 session，不维护 freeze、queue、retry
worker 或 Plugin registry。`MemoryBackendFactory` 在应用启动时根据受信配置构造唯一 bundle；显式配置
失败时 fail closed，不因发现 API key 自动启用，也不静默回退其他后端。

`MemoryNodeBundle` 是封闭的纯 composition object。后续不得向其中增加 `search()`、`save()`、session、
policy、retry、health orchestration 或其他运行时行为；后端专有行为留在 node 闭包和后端私有模块中。
如果新需求无法由现有两个节点表达，应先修改 Graph topology 或另行设计明确节点，不能把 bundle 扩展成
新的 Memory Service/Host。

Graph builder 使用：

```python
bundle = memory_backend_factory.build(config)
graph.add_node("memory_recall", bundle.recall_node)
graph.add_node("memory_commit", bundle.commit_node)
app = graph.compile(checkpointer=checkpointer, store=bundle.store)
```

`store` 的创建、setup、迁移和关闭由 application composition root 拥有。后端 client/model 的关闭也由
bundle 的可选 `aclose` 交还 composition root；这属于资源生命周期，不构成 Memory Runtime。

## 7. State 契约

### 7.1 `memory_context`

建议使用严格、版本化、JSON 可序列化模型：

```python
class MemoryContextItem(BaseModel):
    memory_id: str
    text: str
    source: str
    relevance: float | None = None
    updated_at: datetime | None = None

class MemoryContext(BaseModel):
    schema_version: Literal[1] = 1
    backend_id: str
    status: Literal["ready", "empty", "degraded"]
    snapshot_id: str
    items: tuple[MemoryContextItem, ...] = ()
    issue_codes: tuple[str, ...] = ()
```

约束：

- 每个新 product turn 必须显式覆盖该字段，禁止依赖 reducer 偶然清除上轮内容；
- item 数量、单项字符和总字符有硬上限；
- 正文按不可信历史数据处理，不能提升为 system/developer instruction；
- 不保存 client、Store、session handle、callback、secret、任意 metadata 或媒体正文；
- `snapshot_id` 是快照内容与可信 identity scope 的摘要，用于恢复校验，不是写入幂等键；
- checkpoint 会持久化这些记忆正文，因此生产 checkpointer 必须执行既有访问控制、retention、加密和删除策略。

`memory_id`、`source`、`relevance` 和 `updated_at` 是 recall 标准化、排序、诊断和观测 metadata，不是
assistant reasoning 必须依赖的业务字段。assistant/context compiler 的稳定语义输入只有经过 recall node
排序和裁剪后的 `text` 序列；不得根据 Mem0 score、LangMem ID、后端 source 名称或更新时间编写业务路由、
工具选择和回答规则。未来即使 metadata 字段变化，只要有序 `text` 契约不变，assistant 行为契约仍应兼容。

### 7.2 `memory_commit`

Graph State 另保存本次 commit 的最小结果：

```python
class MemoryCommitState(BaseModel):
    status: Literal["not_requested", "succeeded", "failed", "timed_out", "skipped"]
    memory_event_id: str | None = None
    issue_code: str | None = None
```

State 不保存原始第三方响应或异常文本。错误只保留稳定、脱敏的 `issue_code`。

### 7.3 身份来源

Memory namespace 所需的 user、tenant、agent 等身份必须来自受信 `Runtime` context，不能从用户文本或任意
metadata 构造。State 可以保存用于恢复校验的 opaque identity reference，但节点每次执行仍须与当前
Runtime owner 重新匹配。

## 8. 后端实现

### 8.1 LangMem

LangMem backend 可以构造支持语义索引的 `BaseStore`，由 Graph 原生
`compile(store=store)`。节点从 `runtime.store` 获取同一个 Store：

```text
memory_recall -> runtime.store -> search/manager -> normalize -> memory_context
memory_commit -> runtime.store -> LangMem manager -> MemoryCommitState
```

LangMem 的 extraction、schema、更新、删除和合并语义留在 LangMem manager 内部。若 LangMem 要求
LangChain `BaseChatModel`，composition root 负责提供兼容 model 或薄 Provider adapter；该适配不能演变成
新的项目级 Memory SDK。

### 8.2 Mem0

Mem0 不实现 `BaseStore`。factory 构造 Mem0 client，并通过节点闭包或非 checkpoint 的 Runtime context
注入：

```text
memory_recall -> Mem0 SDK/API -> normalize -> memory_context
memory_commit -> Mem0 SDK/API -> MemoryCommitState
```

`graph.compile()` 不为 Mem0 传 Store。Mem0 的事实提取、更新、合并、embedding 和持久化仍由 Mem0 自身
负责。首版每个新 product turn 执行一次 recall，不再保留 `open_session()` 时固定整个 session baseline
的旧语义；同一 turn 的 ReAct iterations 只消费 State 中冻结的结果。

### 8.3 其他后端与 disabled backend

其他后端直接构造两个符合 State 契约的节点，可自行使用数据库、HTTP API 或 SDK。禁用长期记忆时仍装配
显式 `disabled` bundle：recall 返回 `empty` context，commit 返回 `skipped`，Graph topology 不改变。

## 9. 调用类型与 time travel

| invocation kind | recall | context 来源 | commit |
| --- | --- | --- | --- |
| 新 product `invoke` | 是 | 当前后端的新快照 | 发布回答后一次 |
| `resume` | 否 | checkpoint 中原 turn 快照 | 仅首次到达 product terminal 时一次 |
| 精确 `replay` | 否 | 所选历史 checkpoint 的快照 | 否 |
| 默认 `fork` | 否 | 所选历史 checkpoint 的快照 | 否 |
| 显式 refresh fork | 是 | 当前后端的新快照 | 默认仍否 |

精确 replay 重新查询当前长期记忆会产生 memory drift，因此禁止。默认 fork 也继承历史快照；只有受信调用
显式选择 `refresh_memory` 才重新 recall，并把该分支标记为非精确历史重放。replay/fork 选择的 checkpoint
若尚未包含有效 `memory_context`，精确模式必须 fail closed；refresh fork 可以从 `memory_recall` 重新开始。

`resume` 不重复 recall。等待输入前已经冻结的快照跟随 checkpoint 保留，恢复后继续消费。只有原始
product turn 首次完成时才允许 commit；派生 run ID 不能产生第二次写入。

## 10. Commit 幂等与失败语义

LangGraph checkpoint 只能记录节点执行结果，不能保证外部 API exactly-once。`memory_commit` 必须使用最小
durable ledger：

```text
memory_event_id
backend_id
owner_scope_digest
turn_origin_id
input_digest
status: reserved | invoking | succeeded | failed | outcome_unknown
backend_receipt (可选、受限)
```

`memory_event_id` 从稳定的 logical turn origin、backend 和 commit schema version 推导，不能使用每次
resume 新生成的 invocation run ID。节点执行规则为：

1. 原子 reserve stable event；
2. 已 `succeeded` 时直接返回原结果；
3. 标记 `invoking` 后调用外部后端；
4. 成功后保存受限 receipt 并标记 `succeeded`；
5. 明确失败标记 `failed`；无法判断远端是否成功时标记 `outcome_unknown`；
6. `outcome_unknown` 不自动重试，除非后端支持同一幂等键或可可靠 read-back。

ledger 是外部副作用的业务事实，不是通用 Memory Host。它不得负责 recall、context、队列、session 或后端
选择。即便给 LangGraph node 配置 retry policy，也必须先经过该 ledger。

ledger 的稳定职责只有 `dedup + outcome tracking`。禁止在该组件中增加 retry scheduler、后台 queue、worker、
dead-letter、session lifecycle、后端选择或 context 管理；需要人工处理的 `outcome_unknown` 只暴露受限状态
和运维观测，不由 ledger 自行推进。这样可以防止旧 ingestion Runtime 以“可靠投递”名义重新出现。

commit 采用 best-effort：

- 成功：记录 `succeeded`，Graph 正常结束；
- 后端拒绝或异常：记录稳定 issue，Graph 仍正常结束；
- timeout：记录 `timed_out` 或 `outcome_unknown`，Graph 仍正常结束；
- publish 失败：跳过 commit；
- publish 前取消：不 commit；publish 后取消或 commit 中取消不撤回回答，ledger 保留实际已知状态。

Memory commit 的 latency 和错误不得改写最终回答文本，也不得向用户泄漏 backend 原始错误。

## 11. 发布与终态事件

需要区分三个事实：

```text
response.ready      # compose 已形成规范化最终回答
response.published  # 已写入产品交付流，允许 memory_commit
response.delivered  # Gateway/transport 的实际交付事实，可晚于 commit
```

`memory_commit` 的前置条件是可信的 `response.published` 状态，而不是客户端 ACK。Graph 的 authoritative
terminal 发生在 commit 尝试结束之后，但用户不必等待 commit 才看到答案。所有入口必须持续消费 graph
stream 至终态，不能在收到 final response event 后提前中止 graph，否则 commit 将无法执行。

## 12. 安全、预算与观测

删除 Host 不等于删除必要治理。治理应落在明确所有者中：

- factory：配置 schema、唯一 backend、provider mode、secret resolution 和启动失败；
- recall node：可信 identity、timeout、返回 schema、去重、字符预算和 prompt-safe 投影；
- context compiler：把 memory 标记为不可信历史数据并执行最终 token 预算；
- commit node：稳定输入、timeout、ledger 和错误脱敏；
- composition root：client/store setup、health/readiness 与 shutdown；
- trace：记录 backend ID、节点状态、item 数量、字符数、latency、event ID 和 issue code，不记录 secret、
  原始第三方响应或未裁剪正文。

mock 模式下默认使用显式 disabled/local fake backend，不连接远端。real 模式只有配置完整的 backend 才能
启动；不得因环境中存在 key 自动切换，也不得从真实 backend 静默回退 mock。

## 13. 删除与迁移范围

迁移在原生 LangGraph feat 合并后分阶段完成：

1. 在 `AssistantTurnState` 中加入严格的 `memory_context`、`memory_commit` 和发布屏障字段。
2. 引入 `MemoryNodeBundle`、factory 和 disabled backend，Graph builder 原生装配 recall/commit 节点及可选 Store。
3. 将当前 Mem0 行为迁入直接节点，实现 per-turn recall、publish 后 direct commit 和 ledger。
4. 接入 LangMem backend，验证 `compile(store=...)` 与 `runtime.store` 原生路径。
5. 将 context builder 改为只消费 State 快照，禁止 Runtime 在 Graph 外 prepare/attach memory。
6. 删除不再被真实消费者使用的 `LongTermMemoryService`、`MemoryPluginHost`、Plugin registry、session snapshot、
   ingestion queue 和四生命周期兼容 facade。
7. 同步当前 memory/runtime/gateway authority；历史设计材料继续保留为迁移记录。

删除前必须搜索并处理 session reset、shutdown drain、continuation freeze、媒体引用和 observability 的真实
调用方。不能为了完成类删除而无声丢失仍在使用的产品行为。

## 14. 验证策略

### 14.1 节点契约

- recall 只返回标准化、有界、JSON 可序列化的 `memory_context`；
- commit 只在 `response.published` 后执行；
- assistant/tool 节点没有 backend/store/client 写引用；
- Store/client/secret 不进入 checkpoint；
- 新 turn 明确覆盖旧 `memory_context`。

### 14.2 后端集成

- LangMem graph 同时绑定 checkpointer 与 Store，节点确实通过 `runtime.store` 读写；
- Mem0 graph 不绑定 Store，节点直接使用 fake client 验证请求与标准化结果；
- disabled backend 保持相同 topology 且完全离线；
- 显式配置失败不会静默 fallback。

### 14.3 执行顺序与失败

- 最终回答事件先于 commit 调用可见；
- commit 成功、拒绝、异常和 timeout 均不改变最终回答；
- publish 失败或 publish 前取消不会写长期记忆；
- stream 消费者在看到回答后仍继续驱动 Graph 到 terminal。

### 14.4 恢复与幂等

- resume 复用 checkpoint 中的 context，不重新 recall；
- 同一 logical turn 的 resume 不重复 commit；
- replay/default fork 不 recall、不 commit；
- refresh fork 重新 recall 但默认不 commit；
- commit 节点重跑时 ledger 短路 `succeeded`，并对 `outcome_unknown` fail closed。

测试目录、是否晋升 core invariant 和最小 pytest 范围在实施时按 `tests/README.md` 与项目测试 skill 决定，
不在设计阶段机械指定永久测试文件。

## 15. 验收标准

完成迁移必须同时满足：

1. Assistant Graph 明确包含 `memory_recall` 与 `memory_commit` 节点。
2. LangMem 使用可选原生 `BaseStore`；Mem0 没有 `BaseStore` wrapper。
3. 业务节点只消费 `state.memory_context`，不直接访问 backend。
4. 最终回答先发布，随后 direct commit；commit 失败不影响回答成功。
5. State 中只有有界记忆快照和稳定状态，不含运行对象或 secret。
6. replay/fork/resume 行为符合第 9 节，并有确定性验证。
7. 最小 ledger 能防止常见 resume/retry 重复写入，并明确处理 outcome unknown。
8. `MemoryNodeBundle` 仍只有 node/store/resource composition 字段，没有演化出 Memory Service 行为。
9. assistant 只把有序 memory text 作为稳定语义输入，不依赖后端 metadata。
10. ledger 只承担 dedup/outcome tracking，没有 queue、worker、scheduler 或 session lifecycle。
11. `MemoryPluginHost` 等复杂并行 Runtime 在无真实消费者后删除，不再保留双轨主路径。
12. mock/offline、身份隔离、Tool 治理和 Gateway delivery 边界没有被削弱。
13. 当前 authority 与实际代码、测试一致，文档 authority validator 通过。

## 16. 被否决的替代方案

### 16.1 继续扩展 `MemoryPluginHost`

它能提供统一治理，但会继续维护与 Graph 并行的生命周期、freeze、queue 和 session Runtime，不符合本次减少
自研层的核心目标。

### 16.2 把 Mem0 包装成 `BaseStore`

`BaseStore` 是 namespace/key/value 持久化抽象，Mem0 是带 extraction、merge、update 和检索语义的
Memory Engine。强行包装会泄漏或扭曲两者语义，只为表面统一增加维护成本。

### 16.3 `memory_commit` 只写 outbox，再由外部 worker 调后端

该方案能严格后置到 transport delivery，并天然解耦延迟，但用户已明确选择 direct commit：答案进入产品
交付流后，Graph 节点直接调用 LangMem/Mem0。最小 ledger 只解决幂等，不重新引入 ingestion worker。

### 16.4 把完整长期记忆留在 Runtime context，不进入 State

这样可以减少 checkpoint 正文，但 assistant/tool 节点要么直接依赖 backend，要么需要新的 invocation-local
共享 Runtime，resume 还必须重新查询并产生 drift。当前设计选择保存有界、经过安全投影的 turn snapshot，
换取节点解耦与恢复确定性。
