# 受治理的可编辑上下文层设计

**日期：** 2026-07-13

**状态：** 方向与设计已确认；A+B 首个实施切片已完成自审和独立代码审查

## 1. 目标

在不替换 `AgentGraphRuntime`、`AssistantContextPack`、`PromptCompiler`、`MemoryManager` 和工具治理链路的前提下，为 `assistant_agent` 增加可由人类检查和编辑的个人上下文表面，并逐步补齐 skill 渐进披露、核心记忆容量治理、候选审批和缓存观测。

目标运行链路是：

```text
SOUL.md / USER.md / MEMORY.md / skills/**
        ↓
source adapter / projection service
        ↓
parse / schema validation / identity / safety policy
        ↓
typed ContextSection or governed memory/skill change
        ↓
AssistantContextPack
        ↓
budget / compaction / rendering / context report
        ↓
PromptCompiler
        ↓
provider-native ChatRequest
```

这里的关键不是增加四个 Markdown 文件，而是增加一个受治理的“可编辑来源层”。文件不能成为绕过 memory、tool、identity、policy 或 audit 的新执行路径。

## 2. 已确认的设计决策

1. 保留当前 Context Compiler 主干，不引入 OpenClaw 或 Hermes 的 agent loop。
2. Markdown 只作为 source、projection 或 procedural guidance，不直接在调用点拼接 prompt。
3. `SOUL.md`、`USER.md`、`MEMORY.md` 和 `SKILL.md` 具有不同 authority，不能统一按 system instruction 处理。
4. `USER.md` 和 `MEMORY.md` 不成为与 `MemoryStore` 并列的第二事实源；它们通过 `MemoryManager` 导入、导出或同步。
5. 当前 `MemoryReadPolicy` 不因存在用户文件而失效；普通首次请求仍不自动读取全部长期记忆。
6. skill body 和 reference 的加载不能产生直接 shell、HTTP、browser 或 Provider 执行权；工具执行仍经过 `ActionValidator -> ToolExecutor -> ToolRegistry`。
7. 上下文稳定性使用 `invariant / semi_stable / volatile` 三层模型，不使用过度简化的 stable/dynamic 二分。
8. prompt 注入预算和持久层容量预算分开治理。
9. 超出核心记忆容量时不静默截断持久内容；写入进入合并、替换、审批或可解释拒绝流程。
10. `assistant_candidate` 继续默认不写 durable memory；通用候选审批需要独立的持久状态机。
11. SQLite FTS 或 embedding 不进入第一实现切片；只有本地 retrieval eval 表明现有关键词检索不足时才推进。
12. 默认路径继续是 mock/local/offline；文件能力不会因为检测到外部 Provider key 而改变运行 profile。

## 3. 当前状态与缺口

项目当前已经具备：

- `AssistantContextPack` 统一承载 request、session summary、conversation、memory、realtime task state、plan、observations、tool specs 和 capability descriptors；
- `PromptCompiler` 统一编译生产 Provider 请求；
- `MemoryManager`、`MemoryReadPolicy`、`MemoryWritePolicy`、`MemoryStore`、profile、audit、confirmation 和 retention 边界；
- memory context 独立字符/token 注入预算；
- repo-local `skills/<skill_id>/SKILL.md` descriptor loader；
- tool observation prompt-safe 副本和全局 context 字符预算；
- `MemoryPromotionCandidate` 和 `assistant_candidate` audit-only 默认行为；
- Context Report 和 memory recall/promotion 的脱敏观测。

尚缺：

- 通用的 typed context source/section contract；
- `SOUL.md` 等人类可编辑来源；
- `USER.md` / `MEMORY.md` 与 Memory Service 之间的唯一事实源和同步规则；
- 真正的 skill L0 index、L1 body、L2 references 运行时披露；
- section authority、stability、version 和内部 cache fingerprint；
- user profile/core memory 的持久层容量限制和 consolidation 决策；
- 可跨重启列出、批准、拒绝和过期的 promotion review；
- Provider cache hint 的显式适配与 cache-hit 观测；
- SQLite FTS/embedding 等深历史检索能力。

## 4. 方案比较

### 4.1 方案 A：原始 Markdown 直接拼接

每轮读取 `SOUL.md`、`USER.md`、`MEMORY.md` 和选中的 `SKILL.md`，按固定顺序拼接到 system 或 user prompt。

优点：

- 实现最短；
- 文件编辑后立即生效；
- 容易复刻文件工作空间 Agent 的体验。

缺点：

- 破坏 `AssistantContextPack` 和 `PromptCompiler` 边界；
- 无法可靠表达 identity、authority、provenance、budget 和 trust；
- `USER.md` / `MEMORY.md` 会绕过 Memory Policy；
- 容易引入 prompt injection、双事实源和跨用户泄漏；
- 无法稳定测试 section 选择和裁剪。

此方案拒绝。

### 4.2 方案 B：受治理 source + memory projection

`SOUL.md` 通过只读 source loader 产生 typed section；`USER.md` 和 `MEMORY.md` 通过 projection/sync service 进入 `MemoryManager`；skill 通过受治理的分层 loader 披露。Context Builder 只消费结构化结果。

优点：

- 保留现有 runtime、memory 和 tool 治理边界；
- 支持人类编辑、审计、预算和渐进披露；
- 可以逐阶段实现和回滚；
- local/offline 测试可保持确定性。

代价：

- 需要定义 source contract 和同步语义；
- 文件编辑不是无条件直接生效；
- 必须处理版本冲突、last-known-good 和审批。

这是推荐方案。

### 4.3 方案 C：只提供 API/UI 编辑，不支持文件

所有 persona、profile 和 core memory 编辑都通过 API 或未来 UI，底层只保留数据库。

优点：

- identity 和审计最容易统一；
- 不需要文件同步；
- 更适合多租户服务。

缺点：

- 失去本地优先、可 diff、可备份和可手工修订的工作空间体验；
- UI/API 成为前置依赖；
- 与当前个人助理定位不完全匹配。

此方案保留为多用户部署的长期入口，但不作为本地个人模式的唯一方案。

## 5. 范围拆分

本设计覆盖多个可独立验收的子系统，不能由一个实施计划一次完成。后续必须拆成下列工作包，每个工作包单独形成 implementation plan；涉及高风险行为变化时，应先补对应子 spec。

| 工作包 | 产物 | 是否进入首个实施切片 |
| --- | --- | --- |
| A. Typed Context Source | `ContextSection v1`、source result、section report、builder 接入 | 是 |
| B. SOUL Source | local-only `SOUL.md` loader、schema、预算、last-known-good | 是 |
| C. USER/MEMORY Projection | MemoryManager-backed export/import/diff/approval | 否 |
| D. Progressive Skill Disclosure | L0 index、L1 body、L2 references、受治理读取 | 否 |
| E. Cache Observability | stability partition、内部 fingerprint、Provider usage 观测 | 否 |
| F. Core Memory Governance | storage budget、consolidation、promotion review store | 否 |
| G. Deep History Retrieval | SQLite FTS；embedding 仍需独立证据和 opt-in | 否 |

第一实施计划只覆盖 A+B。它应产生可独立运行、可关闭、无 Memory 行为变化的 `SOUL.md` 能力。

## 6. 核心术语

### 6.1 Context Source

能够在不调用 Provider、不执行工具、不直接读取 MemoryStore 的前提下，产生零个或多个上下文 section 的只读适配器。

Context Source 可以读取显式配置的本地文件，也可以消费已经由 owning service 产生的 prompt-safe snapshot。它不拥有全局 budget、prompt rendering 或持久写入。

### 6.2 Context Section

一次 assistant decision 可消费的、带来源和治理元数据的结构化上下文单元。它不是新的长期存储格式，也不替代 `MemoryItem`、`ContextSummary`、`ToolObservation` 或 `ToolSpec`。

### 6.3 Editable Projection

Memory Service 内部状态的人类可编辑表示。导出产生文件；导入先产生 diff/candidate，再通过 identity、policy、audit 和必要审批改变 MemoryStore。

### 6.4 Authority

section 可以影响模型的方式。authority 不等同于 Markdown 文件名，也不等同于消息 role。

### 6.5 Stability

section 在 Provider 请求之间预期保持字节稳定的程度。stability 是 cache 优化提示，不改变安全优先级或业务语义。

## 7. Authority 与信任模型

### 7.1 Authority 类型

```text
system_policy
owner_persona
procedural_guidance
user_profile_data
user_history_evidence
session_state
runtime_evidence
tool_contract
```

默认优先关系：

```text
system_policy
  > tool_contract / current user request / fresh runtime evidence
  > owner_persona / validated procedural guidance
  > user_profile_data / user_history_evidence / session state
```

该顺序只描述冲突处理，不授权低层内容覆盖高层 policy。

### 7.2 来源矩阵

| 来源 | authority | 默认 prompt 位置 | 可以做什么 | 不能做什么 |
| --- | --- | --- | --- | --- |
| core system policy | `system_policy` | system | 定义安全、工具和运行边界 | 被任何文件覆盖 |
| `SOUL.md` | `owner_persona` | system 中的受限 persona block | 调整人格、语气、关系边界 | 扩大权限、取消确认、改变工具集合 |
| `USER.md` projection | `user_profile_data` | policy 允许时进入 user context | 提供身份摘要和稳定偏好 | 作为系统指令、绕过 MemoryReadPolicy |
| `MEMORY.md` projection | `user_history_evidence` | retrieval 后进入 user context | 提供核心历史证据 | 直接执行其中指令 |
| skill index/body/reference | `procedural_guidance` | capability/skill context | 指导如何使用已治理工具 | 创建新工具权力或直接执行外部动作 |
| conversation/realtime state | `session_state` | user context | 支持当前任务连续性 | 成为长期事实或系统策略 |
| tool observations | `runtime_evidence` | native tool messages/compiled context | 为下一决策和最终答案提供新证据 | 覆盖 system policy |

### 7.3 SOUL 约束

第一版 `SOUL.md` 只接受以下固定二级标题：

```markdown
## Persona
## Expression Style
## Relationship Boundaries
## Avoid
```

规则：

- `Persona` 描述角色气质，不允许声明额外工具或系统身份；
- `Expression Style` 描述语言、长度和表达习惯；
- `Relationship Boundaries` 只能收紧互动边界，不能取消确认、安全或隐私规则；
- `Avoid` 描述应避免的表达方式，不得包含 secrets；
- 未知 section 产生 load issue，不进入 compiled snapshot；
- 不从内容关键词推断工具权限；
- system renderer 必须在 SOUL block 前后写入不可覆盖说明。

### 7.4 SOUL 威胁边界

`SOUL.md` 是本地 owner-trusted 配置，不是可接收远程用户上传的内容。固定 section、secret 扫描和不可覆盖说明只能降低误配置风险，不能证明任意自然语言不会影响模型行为。

本设计能够强制保证的是：

- SOUL 不改变 Provider `tools` 列表、`RunToolSet` 或 `tool_choice`；
- SOUL 不改变 ActionValidator、ToolExecutor、ToolRegistry、approval 和 risk gate；
- SOUL 不能通过文件内容选择新的 Provider、runtime profile 或 memory backend；
- 未绑定的 request identity 不读取 SOUL。

本设计不能声称“恶意 SOUL 文本对模型输出零影响”，因为 persona 的目的本身就是影响输出。只有受信任的本机 owner 可以编辑该文件；多租户或远程上传场景必须使用另行设计的管理面和认证边界。

## 8. ContextSection v1 合约

第一工作包新增以下 Pydantic contract：

```python
ContextAuthority = Literal[
    "system_policy",
    "owner_persona",
    "procedural_guidance",
    "user_profile_data",
    "user_history_evidence",
    "session_state",
    "runtime_evidence",
    "tool_contract",
]

ContextStability = Literal["invariant", "semi_stable", "volatile"]

ContextSectionKind = Literal[
    "soul",
    "user_profile",
    "core_memory",
    "skill_index",
    "skill_body",
    "skill_reference",
    "session_summary",
    "recent_transcript",
    "retrieved_memory",
    "realtime_task_state",
    "plan_state",
    "tool_observation",
    "tool_schema",
    "tool_capability",
]

class ContextSection(BaseModel):
    schema_version: Literal["context_section_v1"] = "context_section_v1"
    section_id: str = Field(min_length=1)
    kind: ContextSectionKind
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    authority: ContextAuthority
    stability: ContextStability
    source_type: Literal[
        "runtime",
        "editable_file",
        "memory_service",
        "skill_loader",
        "tool_registry",
    ]
    source_ref: str = ""
    source_version: str = ""
    identity_scope: Literal[
        "runtime",
        "local_owner",
        "user",
        "project",
        "tenant",
    ] = "runtime"
    priority: int = Field(default=100, ge=0)
    max_chars: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=0, ge=0)
    sensitive: bool = False
    notes: list[str] = Field(default_factory=list)

class ContextSourceIssue(BaseModel):
    code: str = Field(min_length=1)
    source_ref: str = ""
    section_id: str | None = None
    recoverable: bool = True
    public_message: str = Field(min_length=1)

class ContextSourceResult(BaseModel):
    sections: list[ContextSection] = Field(default_factory=list)
    issues: list[ContextSourceIssue] = Field(default_factory=list)
    used_last_known_good: bool = False
```

`content_hash` 不进入公开 Context Report。低熵的个人偏好、文件内容或 memory 文本容易被字典攻击；Provider cache 内部如需 fingerprint，应保留在进程内私有对象或使用不对外暴露的 keyed digest。

### 8.1 Source Protocol

```python
class ContextSource(Protocol):
    def load(self, request: ContextSourceRequest) -> ContextSourceResult:
        raise NotImplementedError
```

`ContextSourceRequest` 只包含：

- runtime-bound `user_id` 及可选 tenant/project scope；
- explicit source root 和绑定的 local owner user ID；
- runtime profile；
- `editable_context_enabled`；
- 每种 section 的字符预算；
- 显式启用的 source IDs。

它不包含 API key、raw Provider response、MemoryStore、ToolRegistry 实例或任意可执行回调。

`source_ref` 只能使用 `editable_context:soul` 这类逻辑引用，不能保存绝对路径。`source_version` 是进程内使用的 opaque version；公开 report 只记录 changed/unchanged，不返回 version 原值。`sensitive=True` 的 section 不允许进入 `AssistantContextPack`，只能产生 issue。

Coordinator 输出必须满足两个额外 invariant：同一 result 内 `section_id` 唯一；`SOUL.md` 最多产生一个组合后的 `kind="soul"` section。重复 ID、空内容和 sensitive section 都转换成 prompt-safe issue，不交给 builder。

### 8.2 AssistantContextPack 兼容策略

第一版采用 additive 接入：

新增字段为：

```python
context_sections: list[ContextSection] = Field(default_factory=list)
```

现有 `conversation_text`、`memory_text`、`observations`、`tool_specs` 等字段继续作为生产行为权威。第一阶段只把 `SOUL.md` section 加入 `context_sections`，不把所有现有字段立即迁移成 section，以避免大爆炸式重构。

后续每迁移一种现有来源，必须证明：

- Provider 可见内容和顺序保持等价，或行为变更经过独立批准；
- budget accounting 不重复计算；
- Context Report 不暴露内容；
- legacy prompt-json 测试兼容边界明确。

### 8.3 加载时机与运行时状态

Context Builder 不在每次 ReAct iteration 中重复读文件。`ContextSourceCoordinator` 由 `ProviderConfig` 显式构造，在一次 run 的首个 assistant decision 前执行一次，并把 prompt-safe `ContextSourceResult` 冻结到 `AgentState.context_source_result`。`build_assistant_context_pack` 只消费该结果。

```text
run entry / AgentGraphRuntime
  -> ContextSourceCoordinator.load_once
  -> AgentState.context_source_result
  -> assistant loop iteration 1..N
       -> build_assistant_context_pack(state)
       -> same frozen context sections
```

这样可以保证：

- 单个 run 的工具循环内 persona 字节稳定；
- 默认关闭时不发生文件 I/O；
- builder 继续聚合已准备数据，不持有路径和文件读取策略；
- PromptCompiler 继续只编译，不读文件；
- checkpoint/state 中只允许保存已验证、`sensitive=False` 的 section 和 prompt-safe issue；
- USER/MEMORY 原文未来仍通过 MemoryManager 路径，不进入通用 file-source state。

### 8.4 PromptCompiler 接口

Builder 完成 persona 预算选择后，PromptCompiler 从 pack 中读取唯一 `kind="soul"` section，并通过以下 additive 参数交给 system prompt policy：

```python
def render_system_instruction(
    profile: SystemPromptProfile = SystemPromptProfile.TEXT_DEFAULT,
    *,
    options: SystemPromptOptions | None = None,
    owner_persona: str = "",
) -> str:
    raise NotImplementedError
```

实际实现中 protocol body 使用正常函数代码；这里的签名固定为 `owner_persona` keyword-only 参数。空字符串必须生成与当前版本逐字节相同的 system instruction。非空时先渲染现有 immutable profile rules，再追加带不可覆盖说明的 owner persona block。PromptCompiler 不解析、裁剪或重新读取 SOUL。

## 9. 本地文件边界

### 9.1 启用条件

第一版只支持显式 local personal mode：

```text
MULTIMODAL_AGENT_EDITABLE_CONTEXT_ENABLED=false
MULTIMODAL_AGENT_EDITABLE_CONTEXT_ROOT=.local/context
MULTIMODAL_AGENT_EDITABLE_CONTEXT_USER_ID=<explicit local user id>
```

默认关闭。启用时必须同时配置绑定的本地 `user_id`。请求 identity 与绑定身份不一致时跳过所有可编辑文件，并记录 prompt-safe issue；不能回退成共享文件。

这些配置名称和默认值是本设计的一部分。implementation plan 必须按现有 `ProviderConfig.from_env` 模式实现，不得另造 request metadata 开关或隐式 fallback。

### 9.2 路径布局

```text
.local/context/
├── SOUL.md
└── projections/
    ├── USER.md
    └── MEMORY.md
```

`projections/` 是后续工作包使用的本机未跟踪数据，不进入仓库。首个工作包只读 `SOUL.md` 并维护 process-local last-known-good；第一版明确不持久化 compiled snapshot，进程重启后遇到非法 SOUL 时直接省略该 section。

### 9.3 文件安全

loader 必须：

- 使用配置 root，不接受 request metadata 提供任意路径；
- `resolve()` 后验证目标仍在配置 root 内；
- 拒绝目录、设备文件和越界 symlink；
- 单次读取固定最大字节数；
- 只接受 UTF-8 文本；
- 先读取完整 bytes，再计算私有版本并 parse，避免同一轮多次读取造成 TOCTOU 内容漂移；
- 扫描 secret-like、raw provider payload、base64/data URI；
- 不在 issue、trace 或 API 中回显原文；
- 导出文件时使用同目录临时文件和原子 replace；
- 不自动修改仓库内的 `AGENTS.md`、`.codex/skills/**` 或业务 `skills/**`。

### 9.4 Last-known-good

`SOUL.md` 的处理规则：

```text
valid file
  -> validate
  -> bounded ContextSection
  -> update process-local last-known-good

invalid/oversized/unsafe file
  -> emit issue
  -> if last-known-good exists, reuse it
  -> otherwise omit SOUL section
```

不允许静默截断 unsafe 或 oversized SOUL。用户必须显式修复文件；last-known-good 的使用会在 Context Report 中以布尔值和错误码显示，但不暴露内容。

last-known-good cache 必须以 resolved context root 和绑定的 local owner user ID 联合分区。它在一次 run 开始时读取并冻结，工具循环中的后续 assistant decision 复用同一 snapshot；文件中途变化只影响下一次 run，避免单个 run 内人格内容漂移。

## 10. USER.md 与 MEMORY.md 投影设计

该工作包不进入首个 implementation plan，但唯一事实源规则在本设计中固定。

### 10.1 所有权

```text
MemoryStore / MemoryManager
        ↓ export
USER.md / MEMORY.md
        ↓ edited by human
projection diff
        ↓ policy / approval / audit
MemoryManager
        ↓ durable write / supersede / delete
MemoryStore
```

runtime 不直接把 `USER.md` 或 `MEMORY.md` 原文注入 prompt。只有 Memory Service 已接受的内容才能通过 `MemoryReadPolicy -> MemoryContextBuilder -> AssistantContextPack` 进入模型上下文。

### 10.2 USER.md

固定 section：

```markdown
## Identity Summary
## Communication Preferences
## Stable Preferences
## Facts
```

导出条目携带不可见的稳定 source ID/version 注释。导入时：

- 新条目产生 `user_explicit` import candidate；
- 修改条目产生 replacement/supersede proposal；
- 删除条目产生 deletion proposal，默认不直接 hard delete；
- identity summary 中的认证主体、tenant、权限和 scope 不允许由文件修改；
- project/tenant-scoped profile 在专门 schema 出现前不写入全局 `user_profile`。

### 10.3 MEMORY.md

`MEMORY.md` 只表示少量 curated core memories，不是完整历史、raw transcript 或 MemoryStore dump。

允许的条目必须：

- 有稳定 memory id/version；
- 来自 identity-visible、unexpired、non-sensitive memory；
- 标明 memory type 和简短 summary；
- 不包含 raw Provider/tool payload、base64、长日志或 secrets。

完整历史继续保存在 MemoryStore，并通过 retrieval 按需查询。

### 10.4 同步与冲突

导入使用 optimistic version check：

- projection version 与 store version 一致才生成变更；
- store 已变化时返回 `projection_conflict`，不自动覆盖；
- 用户重新 export 或通过专门 merge 命令解决；
- 同步必须是 identity-scoped、可 dry-run、可审计；
- 导入失败时 store 不发生部分修改。

文件 watcher、后台自动导入和双向实时同步不进入 v1。第一版使用显式 export/import 操作，避免难以解释的并发写入。

## 11. Skill 渐进披露

当前 repo-local skill descriptor 已具备 permission 和 governed-tool 校验，但缺少完整的运行时层级。

目标层级：

```text
L0: eligible skill index
    name + description + permission summary + content version

L1: skill body
    validated SKILL.md fixed sections

L2: declared reference
    one bounded file under the resolved skill directory
```

### 11.1 L0

- 只列出 enabled、model-invocable、manifest/permission 有效的 skills；
- governed tools 必须已 qualified；
- L0 可发现性不自动改变 exposed/executable tool 集合；
- request 文本不能通过本地关键词规则激活 skill；
- index 受独立字符预算控制，超限时先缩短 description，再按确定性顺序省略并报告。

### 11.2 L1/L2

完整读取必须通过受治理的只读 `skill_view` 能力，或等价的 ToolExecutor-backed loader：

- 输入 skill ID 必须存在于当前 L0 allowlist；
- reference path 必须由 skill manifest/body 显式声明；
- realpath 必须位于 resolved skill directory；
- 文件扩展名、大小、行数和字符数有硬限制；
- tool result 返回 prompt-safe content、version、omitted count 和 issue codes；
- skill body 只能指导使用当前 governed tools；
- 不提供 `run_skill`、任意 shell、任意 HTTP 或任意文件读取。

Skill 内容进入下一次 assistant decision 时使用 `procedural_guidance` authority；它不能覆盖 system policy、tool schema、approval 或当前用户意图。

### 11.3 Skill 写入

自动学习出的 skill 不能直接覆盖业务 `skills/**`。后续 skill learning 使用独立 `SkillChangeProposal`：

```text
candidate
  -> safe draft in pending area
  -> scanner / permission validation / tests metadata
  -> user reviews unified diff
  -> approve
  -> atomic apply
  -> reload with new content version
```

该审批面与短小 memory candidate 分开，因为 skill review 需要完整 diff 和更强权限检查。

## 12. 三层稳定度与缓存纪律

### 12.1 分层

```text
invariant
├── core system policy
├── system prompt profile version
└── validated SOUL snapshot（在文件版本不变时）

semi_stable
├── user/profile snapshot version
├── eligible skill index
├── selected toolset/schema version
└── curated core-memory snapshot

volatile
├── current request
├── recent transcript / session summary
├── retrieved memory
├── realtime task state / plan state
└── tool observations
```

`SOUL.md` 在同一版本内可视为 invariant，但文件一旦修改就形成新的 cache cohort。USER/profile 和 skill/toolset 可能跨多轮稳定，但不能假设跨用户或跨 session 字节相同。

### 12.2 第一阶段只做观测

第一阶段不重排 Provider 消息，也不增加 Provider-specific `cache_control`。先增加：

- section authority/stability accounting；
- source version changed/unchanged；
- invariant/semi-stable/volatile chars；
- Provider 已返回的 cached/prompt token counters（仅当 adapter 安全提供）；
- cache layout version。

公开 trace 不能包含原文、未加密内容 hash、绝对用户文件路径或 memory summary。

### 12.3 Provider cache hints

只有在观测证明有收益、并完成 Provider-specific adapter 设计后才增加：

- cache breakpoint/hint 由 Provider adapter 拥有；
- `PromptCompiler` 只提供稳定分组和版本，不写供应商私有字段；
- 不为命中缓存改变 instruction precedence；
- 不把 volatile user data 提升到 system authority；
- Provider 不支持显式缓存时保持现有 `ChatRequest` 行为。

## 13. 核心记忆容量治理

### 13.1 两种预算

```text
injection budget
  -> 本轮能进入 prompt 的 memory 子集
  -> MemoryContextBuilder / ContextPolicy

storage/core-view budget
  -> user_profile 和 curated core memory 能保留多少常驻摘要
  -> MemoryManager / MemoryWritePolicy / CoreMemoryBudgetPolicy
```

现有 memory context 字符/token 限制继续负责 injection budget。新容量策略不得放进 `AssistantContextPack` builder。

### 13.2 默认值

首个 core-memory governance 子 spec 使用以下起始默认值，并允许本地配置收紧：

| 区域 | 字符上限 | 条目上限 | prompt 注入关系 |
| --- | ---: | ---: | --- |
| compact user profile | 2,000 | 32 | 仍由 memory context budget 选择 |
| curated core memory | 3,000 | 48 | 仍由 read policy 和 retrieval 决定 |

这些不是整个 MemoryStore 的用户配额。完整历史仍受 TTL、retention 和后端容量治理。

### 13.3 满容量行为

```text
new write candidate
  -> deduplicate / supersede check
  -> fits: normal policy path
  -> does not fit:
       produce consolidation proposal
       preserve original store state
       require deterministic merge or user approval
       retry normal policy path after consolidation
```

禁止：

- 静默截断已存 entry；
- 只截 profile summary 却继续无限增长 source list；
- Context Builder 反向修改 MemoryStore；
- 模型自由删除旧记忆为新条目腾空间。

## 14. Promotion Review 状态机

当前 `assistant_candidate` 是 audit-only 结果。后续通用审批需要新的 review contract，不能把 candidate ID 仅放在一次工具结果里。

```text
candidate
  -> preliminary policy rejected: audit only, no review row
  -> pending
       -> approved
            -> policy recheck
                 -> written
                 -> rejected_on_recheck
       -> rejected_by_user
       -> expired
```

review contract 定义为：

```python
class MemoryPromotionReview(BaseModel):
    review_id: str
    user_id: str
    tenant_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    status: Literal[
        "pending",
        "approved",
        "rejected_by_user",
        "expired",
        "written",
        "rejected_on_recheck",
    ]
    redacted_summary: str
    memory_type: str
    source_intent: str
    source_reason: str
    future_use: str
    evidence_summary: str
    candidate_version: int = 1
    expires_at: datetime
    written_memory_id: str | None = None
```

规则：

- 持久层内部保存经过 candidate validator 和 policy 清洗的 `sanitized_candidate`，不得保留 raw transcript、raw evidence、Provider/tool payload 或 secrets；
- API、trace、list 和 audit model 只返回上面的 redacted review view，不返回 `sanitized_candidate`；
- approve 时重新绑定当前 `RequestIdentity`；
- approve 时重新运行最新 `MemoryWritePolicy` 和 `MemoryItem` validation；
- candidate 过期、policy 变化或 scope 不再允许时不能写；
- 重复 approve 必须幂等；
- SQLite/JSONL 后端需跨重启保存 review；
- InMemory 后端只保证 process-local；
- confirmation 和 promotion review 可共享存储接口模式，但不能混用语义和 API 名称。

## 15. 历史检索演进

### 15.1 现状

当前 SQLite 负责持久化，但 retrieval 主要由本地关键词/中文片段策略完成。ConversationStore 是 session continuity，不是完整历史搜索引擎。

### 15.2 FTS 进入条件

只有同时满足以下条件才进入 SQLite FTS 子 spec：

- memory eval 出现可复现的关键词召回不足；
- Recall@k/MRR/false-positive 指标定义了目标和基线；
- FTS5/CJK tokenizer 在目标 SQLite 环境可用性已验证；
- identity、scope、expired、sensitive、superseded filtering 能在 FTS 路径保持；
- schema migration、index rebuild 和 rollback runbook 已设计。

FTS 必须位于 `MemoryStore`/retrieval adapter 后面。Context Builder、prompt renderer 和 assistant loop 不感知 FTS。

### 15.3 Embedding

embedding/vector retrieval 仍是更晚的 opt-in adapter：

- 默认 offline 测试不能依赖网络或新模型；
- 不因检测到 key 自动启用；
- 必须有跨用户泄漏、误召回、敏感注入和 token budget eval；
- 外部 memory core 继续遵守现有 dual-core/remote-service 边界。

## 16. 数据流

### 16.1 首个实施切片

```text
ProviderConfig (explicit local editable context config)
  -> ContextSourceCoordinator.load_once(request_identity)
       -> SoulContextSource.load(source_request)
       -> path/scope check
       -> bounded UTF-8 read
       -> parse fixed sections
       -> secret/payload validation
       -> ContextSection(authority=owner_persona)
  -> AgentState.context_source_result
  -> build_assistant_context_pack(state)
       -> context_sections
       -> section budget/report
  -> PromptCompiler
       -> core system rules
       -> bounded SOUL persona block
       -> existing dynamic user context
```

SOUL 失败不阻断普通 assistant run；它只会被省略或使用 last-known-good。核心 system prompt、tool policy 和运行时默认行为继续可用。

### 16.2 USER/MEMORY 导入

```text
explicit local user action
  -> projection service reads one bounded file
  -> parse + identity binding + version check
  -> dry-run diff
  -> create governed change proposals
  -> user accepts selected proposals
  -> MemoryManager re-evaluates policy
  -> MemoryStore change + audit
  -> export new projection snapshot
```

### 16.3 Skill 读取

```text
AssistantContextPack L0 skill index
  -> LLM emits provider-native skill_view tool call
  -> ActionValidator
  -> ToolExecutor
  -> SkillContextService
  -> bounded L1/L2 content ToolResult
  -> next assistant decision consumes procedural guidance
```

## 17. 错误处理

| 错误 | 行为 | 是否阻断 run |
| --- | --- | --- |
| editable context 默认关闭 | 不加载任何文件 | 否 |
| request identity 不匹配绑定用户 | 跳过文件，记录 `editable_context_identity_mismatch` | 否 |
| 文件不存在 | 省略 section，记录 `source_missing` | 否 |
| UTF-8/格式错误 | 使用 last-known-good 或省略 | 否 |
| 文件越界、symlink 越界、设备文件 | 拒绝并记录安全错误 | 否 |
| 文件超预算 | 不截断持久文件；使用 last-known-good 或省略 | 否 |
| secret/raw payload 检测 | 拒绝 section，不回显内容 | 否 |
| USER/MEMORY projection version 冲突 | 返回 dry-run conflict，不修改 store | 仅阻断同步操作 |
| core memory 容量不足 | 创建 consolidation proposal 或可解释拒绝 | 仅阻断该写入 |
| skill reference 越界/未声明 | ToolResult recoverable error | 否 |
| promotion approve policy recheck 失败 | 标为 `rejected_on_recheck` | 仅阻断该写入 |
| Provider 不支持 cache hint | 忽略 hint，保持普通调用 | 否 |

所有公开 error/trace 只包含稳定错误码、source kind、计数和相对逻辑引用；不包含文件原文、绝对用户路径、memory 内容、Provider raw response 或 secrets。

## 18. 预算与裁剪

### 18.1 第一切片默认值

```text
SOUL file hard byte limit: 16,000 bytes
SOUL file hard limit: 4,000 chars
SOUL compiled section limit: 2,000 chars
SOUL per subsection limit: 800 chars
context source issues retained per build: 16
```

hard limit 超出时拒绝新版本，不静默截断。compiled section 可以通过确定性的 section-level 选择满足 2,000 字符，但必须按完整条目/完整段落选择，不能在语句中间截断；省略数进入 report。

选择顺序固定为 `Relationship Boundaries -> Avoid -> Persona -> Expression Style`，每个 subsection 内保持文件原顺序。先加入不超过 800 字符的完整段落，加入后若超过 2,000 字符则省略该段及其后同优先级段落。这个顺序只影响 owner persona 的预算选择；核心 system policy 永远独立保留。

### 18.2 全局预算关系

- `ContextBudgetReport` 新增 `owner_persona_chars`，并把它计入全局 `total_chars`；
- 未显式设置硬 `context_budget_max_chars` 时，SOUL 不获得无限 headroom；
- 显式硬预算包含 SOUL、动态 context 和 tool schema；
- 超限时先移除低优先级 persona 条目，不先裁新工具证据；
- request、realtime side-effect state 和最新 observation 的既有保留规则不改变；
- 全局 token 强控制仍不是本阶段目标。

## 19. Observability

扩展 Context Report，但不暴露内容：

```text
context_sections
  count_by_kind
  chars_by_authority
  chars_by_stability
  source_issue_count
  source_issue_codes
  used_last_known_good
  source_versions_changed
  omitted_section_count
  cache_layout_version
```

当前 `ContextReport.sections["system_prompt"]` 继续表示最终 system prompt 的聚合大小。SOUL 已包含在该聚合中，因此 `owner_persona` 不再作为另一个可相加的 top-level report section；它只出现在 `context_sections` source summary 中。`ContextReport.total_chars` 必须按最终 Provider payload 计算，不能把 SOUL 在 `system_prompt` 和 source summary 中重复相加。

SOUL section report 可记录：

- included；
- chars/tokens estimate；
- selected paragraph count；
- omitted paragraph count；
- source=`editable_context:soul`；
- version changed=true/false；
- compacted/trimmed；
- issue codes。

不得记录：

- SOUL 原文或摘要；
- 文件绝对路径；
- content hash；
- USER/MEMORY 内容；
- tool/provider raw payload；
- secret-like data。

## 20. 首个实施切片文件结构

以下是后续 A+B implementation plan 的建议范围，不在本设计阶段修改源码。

Create：

- `src/assistant_agent/services/context/sources.py`：`ContextSource` protocol、source request 和 `ContextSourceCoordinator`；
- `src/assistant_agent/services/context/soul_source.py`：SOUL path、read、parse、validate、last-known-good；
- `tests/test_context_sources.py`：contract、identity、path、budget、issue 行为；
- `tests/test_soul_context_source.py`：合法/非法/超限/secret/symlink/last-known-good；

Modify：

- `src/assistant_agent/schemas/context.py`：新增 section/source issue contracts 和 additive pack 字段；
- `src/assistant_agent/config.py`：显式 opt-in 配置；
- `src/assistant_agent/agent/state.py`：保存一次 run 内冻结的 prompt-safe `context_source_result`；
- `src/assistant_agent/agent/runtime.py`：构造 coordinator，并在首个 assistant decision 前加载一次；
- `src/assistant_agent/services/context/builder.py`：消费 state 中已加载的 sections，计算预算和 report 输入；
- `src/assistant_agent/agent/system_prompt_policy.py`：在 immutable runtime rules 之后渲染受限 persona block；
- `src/assistant_agent/services/context/prompt_compiler.py`：只消费已验证 persona material，不读文件；
- `src/assistant_agent/services/context/report.py`：section authority/stability accounting；
- `tests/test_assistant_context_renderer.py`：SOUL 与动态数据的 authority/预算顺序；
- `tests/test_prompt_compiler.py`：SOUL block、关闭时等价行为；
- `tests/test_provider_config_validation.py`：默认关闭和绑定 identity；
- `docs/context_engineering_status.md`：实现后更新当前状态；
- `docs/memory-service-architecture.md`：只补文件投影仍必须经过 MemoryManager 的边界说明，不宣称 USER/MEMORY 已实现；
- 若 `docs/prompt-engineering-architecture.md` 已落地，则同步记录 persona source/PromptCompiler 边界。

不修改：

- `tools/memory_tool.py`；
- concrete MemoryStore；
- ToolExecutor/ActionValidator；
- realtime Gateway 协议；
- Provider adapter 的 cache-specific 字段；
- repo workflow `.codex/skills/**`；
- 业务 `skills/**`。

## 21. 测试策略

### 21.1 Contract 单元测试

- 合法 section 可序列化，authority/stability 枚举严格；
- 空 `section_id`、未知 authority、负 budget 被拒绝；
- ContextSourceResult 允许 recoverable issues 和零 section；
- AssistantContextPack 无 `context_sections` 输入时与当前行为等价。

### 21.2 SOUL loader 测试

- 默认关闭时不触碰文件系统；
- configured root 下合法文件被解析；
- request identity 不匹配时不读取文件；
- 缺失文件、未知 section、非法 UTF-8、超字节/字符、secret-like 内容产生稳定错误码；
- 越界 symlink、目录和设备文件被拒绝；
- 修改合法文件后 version changed；
- 非法新版本复用 last-known-good；
- first load 非法时省略 section；
- issue/trace 不包含原文和绝对路径。

### 21.3 Builder/Compiler 测试

- SOUL 只在显式启用且 identity 匹配时进入 pack；
- SOUL 不改变 prompt tool set、tool choice 或 RunToolSet；
- SOUL 无法通过 Markdown 声明新增工具；
- system 核心 policy 始终位于 SOUL 之前；
- SOUL 与 conversation/memory/observation 的 authority label 正确；
- 显式硬 context budget 包含 persona；
- 超预算优先省略低优先 persona 条目，不删除最新关键 observation；
- native tool、native final-only、summary final-only 的既有契约保持；
- default config 生成的 Provider request 与当前 characterization fixture 完全相同；
- 单个 run 的多次 assistant decision 复用同一 frozen SOUL，即使文件在 run 中途变化；
- 下一次 run 才观察到合法的新 SOUL 版本。

### 21.4 安全与回归

- prompt 中的 memory/conversation/tool output 仍被标为 data；
- SOUL 中的 `ignore policy`、`use shell directly` 等文本不能改变工具 exposure/execution；
- cross-user request 不共享 SOUL；
- raw provider/base64/secret 不进入 prompt、trace 或 last-known-good；
- mock/local/offline 全部通过，无真实 Provider 调用。

### 21.5 建议验证命令

实现 A+B 后至少运行：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_context_sources.py \
  tests/test_soul_context_source.py \
  tests/test_assistant_context_renderer.py \
  tests/test_prompt_compiler.py \
  tests/test_provider_config_validation.py -q

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_manager.py \
  tests/test_memory_context_builder.py \
  tests/test_memory_tool_boundary.py -q

git diff --check -- \
  AGENTS.md \
  docs/context_engineering_status.md \
  docs/memory-service-architecture.md \
  docs/prompt-engineering-architecture.md \
  docs/superpowers/specs \
  src/assistant_agent/schemas/context.py \
  src/assistant_agent/services/context \
  tests
```

如果 prompt-engineering authority 文件尚未创建，`git diff --check` 中不应凭空加入该路径；implementation plan 必须以执行时工作树为准。

## 22. 迁移策略

### Phase 0：Contract only

- 新增 `ContextSection v1` 和 report 字段；
- pack additive 字段默认为空；
- 无 Provider 可见变化；
- characterization 测试证明默认请求等价。

### Phase 1：SOUL local opt-in

- 默认关闭；
- 只支持绑定单一 local user；
- 固定 schema、预算和 last-known-good；
- 无 USER/MEMORY/skill 行为变化。

### Phase 2：USER/MEMORY projections

- 先做 export 和 dry-run diff；
- 再做显式 import proposal；
- 最后接审批和原子 apply；
- runtime 始终从 MemoryManager 读，不直接读 projection。

### Phase 3：Progressive skills

- 先扩 L0 观测和预算；
- 再增加受治理的 L1/L2 view；
- 最后增加独立 skill change approval；
- 不同时引入语义 tool recall。

### Phase 4：Core memory governance

- 增加 storage budget 和 consolidation proposal；
- 增加持久 promotion review；
- 保持 `allow_auto_write=False` 默认值。

### Phase 5：Cache optimization

- 先收集 section/cache usage；
- 再为明确支持的 Provider 加 adapter hint；
- Provider 不支持时无行为差异。

### Phase 6：Deep history retrieval

- 由 eval 触发 SQLite FTS 子项目；
- embedding/vector 继续独立 opt-in。

## 23. 回滚

- Phase 0 additive contract 可通过不填 `context_sections` 实现行为回滚；
- Phase 1 关闭 `MULTIMODAL_AGENT_EDITABLE_CONTEXT_ENABLED` 即完全停止文件读取；
- 删除或修复 SOUL 不修改 MemoryStore；
- USER/MEMORY import 必须保留 audit 和 supersede 链，不能用文件覆盖回滚数据库；
- skill view 是只读能力，关闭 tool exposure 即停止运行时加载；
- cache hint 只能在 Provider adapter 中关闭，不改变 Context Pack；
- FTS 路径必须保留原关键词策略作为可配置回退，schema migration 需单独 runbook。

## 24. 非目标

本总设计不授权以下实现：

- 新的 agent loop、planner/controller 或大单体 AIAgent；
- 把 OpenClaw/Hermes runtime 引入项目；
- 默认自动注入全部 USER/MEMORY；
- Markdown 中声明工具、权限、API key 或任意代码执行；
- 自动从 request 文本用关键词/vector 覆盖 LLM 的 memory source intent；
- 默认 LLM summary、自动 prompt 优化、在线 RL；
- 自动修改 `.codex/skills/**` 或仓库指令；
- 多租户共享个人文件根目录；
- 真实 Provider smoke；
- 新依赖、外部 vector DB 或联网安装。

## 25. 验收标准

总设计方向成立需满足：

1. 人类可编辑性通过受治理 source/projection 实现，而不是原始字符串拼 prompt。
2. `MemoryManager` 继续拥有 USER/MEMORY 的 durable truth、policy、profile、audit 和 identity。
3. `PromptCompiler` 只消费已解析材料，不读目录、不查 MemoryStore、不执行工具。
4. 工具和 skill 执行继续经过 ActionValidator、ToolExecutor、ToolRegistry 和 policy。
5. section authority、stability、budget、source issue 和脱敏 report 可测试。
6. 文件能力默认关闭，local identity 显式绑定，跨用户请求 fail closed。
7. unsafe/oversized 文件不静默截断并生效；last-known-good 行为可观察。
8. USER/MEMORY projection 导入是显式、版本化、可 dry-run、可审计、原子化的操作。
9. core memory storage budget 与 prompt injection budget 分离。
10. promotion approve 时重新运行 identity 和最新 policy，不把旧审批当永久授权。
11. cache 优化不改变 authority 和消息语义；Provider 不支持时保持兼容。
12. FTS/embedding 只在 eval 证据支持时进入独立开发阶段。

首个 A+B 实施切片完成还需满足：

1. 默认配置下 Provider 请求与现状等价。
2. 合法 SOUL 在显式 local opt-in 下进入受限 persona block。
3. SOUL 不能改变工具集合、审批、memory policy 或 runtime profile。
4. identity/path/secret/budget/last-known-good 测试通过。
5. Context Report 只暴露 section 计数、大小、版本变化和稳定错误码。
6. 相关 context/prompt 权威文档、测试和源码在同一开发阶段通过验证后统一提交。

## 26. 后续文档与计划边界

用户复核本设计后，下一步只为 A+B 编写 implementation plan：

```text
ContextSection v1
+ ContextSource protocol
+ local opt-in SoulContextSource
+ pack/report/compiler integration
+ focused tests and authority-doc updates
```

以下工作必须在进入实现前各自产生更窄的子 spec 或明确批准的 implementation plan：

- USER/MEMORY projection sync；
- progressive skill disclosure；
- promotion review 与 core memory consolidation；
- Provider cache hints；
- SQLite FTS/embedding retrieval。

设计文档不单独提交。根据仓库 `AGENTS.md`，应在对应开发阶段完成代码、测试和权威文档更新并通过验证后统一提交。

## 27. 自审记录

本节记录设计阶段的静态自审，不替代实施阶段的代码审查、测试和安全验证。

### 27.1 已检查并收敛的问题

| 检查项 | 发现的风险 | 当前收敛结果 |
| --- | --- | --- |
| 编译器边界 | source loader 可能退化为 builder 内直接读文件 | 明确由 runtime/coordinator 每个 run 加载一次并冻结，builder 只消费结构化结果 |
| durable truth | USER/MEMORY 文件可能形成第二真相源 | 明确为 projection；MemoryManager/store 继续拥有唯一 durable truth |
| 权限提升 | SOUL 或 skill Markdown 可能改变工具、审批或 Provider | editable content 只进入受限 section；不能改变 RunToolSet、tool choice、validator、policy、profile |
| identity | 本地文件根目录可能由请求元数据动态选择 | root 与 owner id 只允许来自显式进程配置；请求身份不匹配时 fail closed |
| 运行一致性 | 同一 run 内文件变化可能造成前后节点看到不同内容 | source result 在 run 创建时冻结；后续节点不重复读文件 |
| 无效更新 | 文件解析失败后可能静默清空已生效人格 | 使用 process-local、按 root + owner 隔离的 last-known-good，并报告稳定错误码 |
| 截断语义 | oversized 内容被静默截断后可能改变边界含义 | 文件级超限拒绝；合法 section 的编译预算按固定优先级确定性选择 |
| prompt 重复计量 | persona 同时计入 system prompt 和新增 section 可能双重收费 | ContextReport 保留当前 system aggregate；persona source summary 仅作非累加观测 |
| cache 承诺 | stable 标签可能被误解为已有 Provider cache 命中保证 | 第一阶段只分类和观测；adapter cache hint 单独立项且必须由 Provider 能力证明 |
| 审批时序 | candidate 初检通过后，审批可能绕过最新 policy | approve/apply 前重新校验 identity、版本和最新 policy；失败进入 rejected_on_recheck |
| 敏感信息 | trace/report 可能泄露文件正文、绝对路径或 source hash | 只暴露逻辑 source id、字符数、版本变化和稳定错误码；敏感 section 不进入 report |
| 范围膨胀 | 总设计可能一次性授权七个 phase | 首个实施计划硬限制为 A+B；其余 phase 需要独立子 spec 或明确批准 |

### 27.2 与当前权威边界的一致性结论

- 不改变 `AgentGraphRuntime` / assistant loop 的主运行时地位；
- 不绕过 ActionValidator、ToolExecutor、ToolRegistry、policy 与 audit；
- 不把检索、profile merge 或文件读取放进 `AssistantContextPack` builder；
- 不改变 `MemoryManager`、MemoryReadPolicy、MemoryWritePolicy 和 store 的职责；
- 不让 PromptCompiler 读取外部状态，只允许其渲染已经验证且有预算的 section；
- 默认配置仍为 mock/local/offline 且 editable context 关闭；
- 不引入新依赖，不要求真实 Provider，不触碰 realtime/Gateway 主边界。

### 27.3 仍需实施阶段验证的风险

1. 当前 system instruction 的字节级等价需要 characterization test 证明，不能只依赖代码审阅。
2. `AssistantContextPack`、`AgentState` 和 trace schema 的 additive 字段是否影响现有序列化快照，需要针对现有测试夹具验证。
3. 16,000 bytes、4,000 chars、2,000 compiled chars 等首版预算是保守初值，需要通过离线 case 和 context report 数据调整。
4. secret scanner 的规则必须复用或对齐现有 redaction 语义，避免把普通人格文本误判为 secret；具体规则在 implementation plan 中定位到现有实现后确定。
5. process-local last-known-good 在多进程部署中不提供跨 worker 一致性；首版接受这一限制，若实际部署需要再设计持久、签名化 snapshot。
6. owner-trusted SOUL 仍会影响模型表达，架构只能保证它不改变受治理能力边界，不能承诺任意恶意人格文本对生成内容零影响。

### 27.4 自审结论

本设计在架构职责、身份隔离、权限边界、预算语义和渐进迁移方面不存在已知的阻断性矛盾，可以进入用户复核。它尚未授权实现；用户确认后，下一份 implementation plan 仍只覆盖 A+B，并必须把 27.3 的前两项转化为首批验证任务。
