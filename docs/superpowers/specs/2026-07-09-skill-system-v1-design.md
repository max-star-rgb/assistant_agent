# Skill System v1 Deepening Design

## Goal

把 Phase 3 从“已有 repo-local skill capability catalog”做深到第一版可长期维护的 Skill System v1：

```text
skills/<skill_id>/SKILL.md
        |
        v
Skill Loader
        |
        v
Skill Registry / Exposure Report
        |
        v
Context Capability Catalog
        |
        v
LLM selects normal governed tool
        |
        v
ActionValidator -> ToolExecutor -> ToolRegistry
```

Skill v1 的目标不是让 skill 自己执行任务，而是让系统能安全地描述“某个能力由哪些受治理工具组成、什么时候给模型看到、为什么被启用或跳过”。实际执行仍然只有工具系统一条路。

## Current State

当前仓库已经具备 Phase 3 的初始实现：

- `skills/<skill_id>/SKILL.md` 是 repo-local 业务 skill 入口。
- `src/assistant_agent/services/context/skill_loader.py` 已加载 frontmatter 和固定 prompt-safe sections。
- `SkillDescriptor` 已包含 `name`、`description`、`enabled`、`disable_model_invocation`、`governed_tools`、`permissions`、`required_inputs_by_tool`、`when_to_use`、`when_not_to_use`、`safe_examples`、`runtime_constraints`。
- `## Permissions` 已要求每个 governed tool 有对应 `tool:<tool_name>`。
- disabled、manual-only、缺失描述、缺失 governed tools、缺失 tool permission、name mismatch 等情况已产生 `SkillLoadIssue`。
- `src/assistant_agent/services/context/capability_catalog.py` 已把有效 skill 转换为 `ToolCapabilityDescriptor`。
- capability catalog 只在 governed tools 已注册且已进入 prompt tool subset 时暴露 skill。
- native context 已能渲染 skill-style capability catalog，而不重复渲染完整 ToolSpec。
- `skills/realtime_web_search/SKILL.md` 是当前唯一 repo-local 业务 skill。
- `tests/test_phase3_skill_system_gate.py` 已固定第一层 gate：manifest、permission、tool mapping、disabled/under-permissioned audit、无 `run_skill` 直接执行路径。

所以 Phase 3 深化不是从零实现 skill 系统，而是补齐三个缺口：

1. Skill exposure 的报告还不够一等。
2. 本地 skill override 与内置 fallback 的禁用语义需要收紧。
3. 还需要覆盖真实 LLM/native tool call 到 `ActionValidator -> ToolExecutor` 的合同路径。

## Non-Goals

本阶段不做：

- Skill marketplace。
- 用户上传 skill。
- 社区 skill 安装、审核、评分或分发。
- `run_skill` 工具。
- skill 直接调用 Python、shell、HTTP、browser、MCP 或 provider。
- workflow engine。
- skill 自己拥有 memory schema。
- skill 自己拥有 eval runtime。
- skill 自己拥有 agent routing 或 multi-agent fabric。
- skill 运行时沙箱。
- 远程 skill registry。
- 按用户动态生成 skill。
- ASR/TTS 或媒体服务集成。

Phase 3 v1 只处理本仓库内、受版本控制的、prompt-safe 能力描述。

## Design Options

### Option A: Keep Current Capability Catalog Only

继续使用当前 `SkillDescriptor -> ToolCapabilityDescriptor`，只依赖 `selection_reasons` 做 debug。

优点是改动最少。缺点是后续调试时很难回答：

- 这个 skill 是从 repo 加载的，还是内置 fallback？
- 为什么一个 skill 被跳过？
- 一个本地 disabled skill 是否真的阻止了同名内置 fallback？
- 模型看到 skill 描述之后，实际工具调用是否仍经过治理链路？

### Option B: Deepen Skill v1 Inside Existing Context/Tool Boundaries

在现有 loader 和 capability catalog 内补一层清晰的 registry/exposure report，不新增 skill runtime。

核心做法：

- repo-local skill 名称一旦出现，就成为该 skill id 的 authoritative override。
- disabled、manual-only、invalid 或 under-permissioned 的 repo-local skill 不能被同名 built-in fallback 重新暴露。
- selection output 显式报告 loaded、selected、skipped、fallback、permission issue、tool availability issue。
- 加合同测试证明：skill 只影响 prompt/context，LLM/native tool call 仍调用普通 ToolSpec，并经过 `ActionValidator -> ToolExecutor -> ToolRegistry`。

### Option C: Build A Separate Skill Runtime

新增 `SkillRuntime`、`run_skill`、workflow step、skill memory schema 和独立执行链。

这会让 Skill 变成第二套 Tool System，并直接破坏当前项目最重要的治理边界。它长期也许有吸引力，但不适合当前一个开发者阶段。

### Recommended Approach

采用 Option B。

Phase 3 v1 的实现位置仍然是：

- `src/assistant_agent/services/context/skill_loader.py`
- `src/assistant_agent/services/context/capability_catalog.py`
- `src/assistant_agent/schemas/context.py`
- `tests/test_skill_loader.py`
- `tests/test_tool_catalog.py`
- `tests/test_assistant_context_renderer.py`
- `tests/test_phase3_skill_system_gate.py`
- 必要时补一条 native tool call contract test。

不创建新的 service process，不创建新的 runtime graph，不创建 skill executor。

## Core Vocabulary

### Skill Manifest

`skills/<skill_id>/SKILL.md` 是 skill manifest。

v1 允许的输入字段只包括：

- frontmatter:
  - `name`
  - `description`
  - `enabled`
  - `disable-model-invocation`
- fixed sections:
  - `## Governed Tools`
  - `## Permissions`
  - `## Required Inputs`
  - `## When To Use`
  - `## When Not To Use`
  - `## Safe Examples`
  - `## Runtime Constraints`

其他正文 section 不进入 prompt，不进入 descriptor，不参与执行。

### Skill Descriptor

`SkillDescriptor` 是 loader 输出的 prompt-safe 结构化数据。

它不是工具，不是 agent，不是 workflow，也不是可执行代码。它只说明：

- 这个 skill 叫什么。
- 它描述什么能力。
- 它依赖哪些 governed tools。
- 它声明哪些 permissions。
- 它什么时候适合被提示给模型。
- 它有什么运行约束。

### Permission

v1 权限词汇只允许 `tool:<tool_name>`。

这不是用户授权系统，也不是 OAuth scope。它只是一个本地 manifest guard，用来防止 skill 声称自己会用某个工具，但没有显式声明工具权限。

v1 规则：

- 每个 governed tool 必须有对应 `tool:<tool_name>`。
- `tool:<tool_name>` 必须能映射到已注册 ToolSpec，才能最终进入 prompt context。
- 非 `tool:` 前缀的 permission 不授予能力。
- 非 `tool:` 前缀 permission 不应渲染给模型；更稳妥的行为是产生 prompt-safe issue 并跳过该 skill。

### Skill Registry / Exposure Report

Skill v1 需要一个一等 debug 产物。它可以先是现有 `ToolCapabilityCatalogSelection.selection_reasons` 的结构化升级，不需要新服务。

建议字段：

```text
skill_report_v1:
  loaded_skill_ids
  selected_skill_ids
  skipped:
    - skill_id
      reason
  builtin_fallback_skill_ids
  override_skill_ids
  governed_tool_names
  permission_issue_count
  unavailable_tool_count
```

报告原则：

- 不包含原始 `SKILL.md` 全文。
- 不包含用户原始长文本。
- 不包含 API key、provider raw payload 或外部返回。
- 只记录 skill id、issue code、tool name、permission name 等 prompt-safe 信息。

### Tool Capability Descriptor

`ToolCapabilityDescriptor` 是给模型看的能力目录。

它可以描述一个 skill，但不能扩大工具权限。模型最终仍然只能调用 provider-native tools 或 legacy tool call 中暴露的普通 ToolSpec。

### Built-in Capability

当前 `capability_catalog.py` 有 `_DEFAULT_CAPABILITIES`，用于在 repo-local skill 不存在时提供内置 capability。

Phase 3 深化要明确：

- repo-local skill 不存在时，允许使用 built-in fallback。
- repo-local skill 有效时，repo-local 覆盖 built-in。
- repo-local skill disabled、manual-only、invalid 或 under-permissioned 时，同名 built-in 也必须被 suppressed。

否则本地禁用 skill 会被 fallback 悄悄重新打开。

## Target Architecture

```text
Repository
  skills/<skill_id>/SKILL.md
        |
        v
Skill Loader
  - parse fixed sections only
  - validate name/description/enabled
  - validate governed tools declaration
  - validate tool:<name> permissions
  - emit SkillDescriptor or SkillLoadIssue
        |
        v
Candidate Skill Set
  - repo descriptors
  - repo issue skill ids
  - built-in fallback descriptors
        |
        v
Capability Catalog
  - suppress fallback when repo skill id exists with issue
  - require governed tools are registered
  - require governed tools are prompt-selected
  - produce ToolCapabilityDescriptor
  - produce skill_report_v1
        |
        v
AssistantContextPack
  - prompt_tool_specs
  - tool_capabilities
  - context report / selection report
        |
        v
Provider-native tool calling
  - model sees ordinary ToolSpec schemas
  - model sees optional skill-style guidance
        |
        v
ActionValidator
        |
        v
ToolExecutor
        |
        v
ToolRegistry
```

## Runtime Flow

### 1. Load

At context-build time:

```text
load_repo_skill_descriptors(repo_root)
```

Loader only reads `skills/`, never `.codex/skills/`.

Accepted skill:

- frontmatter exists.
- `name` matches directory name.
- `description` exists.
- `enabled` is true or omitted.
- `disable-model-invocation` is false or omitted.
- `## Governed Tools` has at least one tool.
- `## Permissions` contains `tool:<tool_name>` for every governed tool.
- every permission is in the v1 allowed vocabulary.

Rejected skill:

- creates `SkillLoadIssue`.
- does not create `SkillDescriptor`.
- still records its `skill_id` as a repo-local override source, so same-name fallback cannot reappear silently.

### 2. Select

Capability catalog receives:

- available ToolSpecs from `ToolRegistry`.
- prompt-selected ToolSpecs from `tool_catalog`.
- loaded SkillCatalog.

It selects a skill only when:

- the skill descriptor is valid.
- all governed tools exist in available ToolSpecs.
- at least one governed tool is in prompt-selected ToolSpecs.
- no skip condition applies.

This keeps skill selection tied to existing tool recall. Skill does not become a second intent router.

### 3. Render

Context renderer may show the selected capability catalog to the model.

Renderer must not show:

- raw `SKILL.md` body.
- unknown sections.
- shell/browser/http instructions from arbitrary sections.
- secret-like values.
- provider raw payloads.
- direct execution instructions.

Renderer may show:

- skill name.
- description.
- governed tool names.
- `tool:<name>` permissions.
- required inputs.
- when-to-use / when-not-to-use guidance.
- runtime constraints, including “execute governed tools only through ToolExecutor”.

### 4. Execute

There is no skill execution step.

The model either:

- calls a normal native tool from `ChatRequest.tools`, or
- emits a legacy tool call compatible with the current assistant loop.

Then the existing runtime path applies:

```text
AssistantDecision / NativeToolCall
        |
        v
ActionValidator
        |
        v
ToolExecutor
        |
        v
ToolRegistry
```

Skill metadata must not call `registry.run(...)`, import tools directly, or construct provider clients.

## Relationship To Realtime

Realtime Phase 1 already established the loop:

```text
Gateway Session
  -> Realtime turn/run state
  -> AgentGraphRuntime
  -> ToolExecutor
  -> streamed response / cancel / interrupt
```

Skill v1 does not add realtime-specific routing.

For realtime:

- Gateway does not select skills.
- Realtime session does not hold skill state.
- `AgentGraphRuntime` receives the same context pack path as chat.
- interrupt/cancel semantics do not change.
- if a cancelled or stale turn produced a tool output, skill metadata does not promote or persist anything.

Skill only changes what prompt-safe capability guidance may appear in context.

## Relationship To Memory

Skill v1 does not own memory schema.

Allowed:

- A future skill may declare `memory_retrieval` or `memory_save` as governed tools, if it also declares `tool:memory_retrieval` or `tool:memory_save`.
- Actual memory read/write still goes through `ActionValidator -> ToolExecutor -> memory tool -> MemoryManager`.
- `MemoryReadPolicy` and `MemoryWritePolicy` remain authoritative.

Not allowed:

- Skill creates memory directly.
- Skill updates `user_profile`.
- Skill defines durable memory schema.
- Skill bypasses `source_intent`.
- Skill-scoped or tenant/project-scoped memory updates global `user_profile`.

If a future skill needs memory-specific behavior, that is a separate design after Memory v1 proves the need.

## Hermes / OpenClaw Boundary

Borrow:

- capability manifest idea.
- prompt-safe skill descriptions.
- skill as product-facing capability grouping.
- trajectory/debug visibility for selected or skipped capabilities.

Do not borrow:

- large monolithic agent that owns gateway, memory and tools together.
- gateway that holds the brain.
- plugin marketplace.
- tool execution directly from skill code.
- RL or learning loop tied to skill selection in v1.

The local project remains:

```text
Gateway
  -> AgentGraphRuntime
  -> Context + Tool Capability Catalog
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
  -> Memory / Providers / Local Services
```

## Trace And Observability

Phase 3 should make one question easy to answer:

> 用户这轮请求里，模型为什么看到了这个 skill，又为什么最终只调用了普通工具？

Minimum report:

```text
skill_report_v1:
  loaded_skill_ids: ["realtime_web_search"]
  selected_skill_ids: ["realtime_web_search"]
  skipped: []
  builtin_fallback_skill_ids: []
  override_skill_ids: ["realtime_web_search"]
  governed_tool_names: ["web_search"]
  permission_issue_count: 0
  unavailable_tool_count: 0
```

For rejected local override:

```text
skill_report_v1:
  loaded_skill_ids: []
  selected_skill_ids: []
  skipped:
    - skill_id: "realtime_web_search"
      reason: "skill_disabled"
  builtin_fallback_skill_ids: []
  override_skill_ids: ["realtime_web_search"]
  governed_tool_names: []
  permission_issue_count: 0
  unavailable_tool_count: 0
```

The second case matters because same-name built-in fallback must not revive a disabled local skill.

## Required Contract Tests

Phase 3 implementation should have these gates.

### Loader Contract

Cover:

- valid skill manifest loads.
- disabled skill produces `skill_disabled`.
- manual-only skill produces `model_invocation_disabled`.
- missing governed tools produces `missing_governed_tools`.
- missing `tool:<name>` permission produces `missing_tool_permission`.
- unknown permission prefix produces `invalid_permission`.
- unallowed body sections do not appear in descriptor JSON.
- `.codex/skills` is ignored.

### Capability Catalog Contract

Cover:

- valid repo-local skill is selected when governed tool is available and prompt-selected.
- skill is skipped when governed tool is unavailable.
- skill is skipped when governed tool is available but not prompt-selected.
- repo-local skill overrides same-name built-in descriptor.
- disabled same-name repo-local skill suppresses built-in fallback.
- invalid same-name repo-local skill suppresses built-in fallback.
- selection produces prompt-safe skill report fields.

### Context Rendering Contract

Cover:

- native context renders skill-style capability catalog.
- native context does not render full ToolSpec list when native tools are available.
- rendered skill catalog includes governed tools and permissions.
- rendered context omits raw unallowed sections.
- context budget report counts tool capability chars.

### Tool Execution Contract

Cover:

- scripted native LLM receives normal ToolSpec schemas and optional skill capability context.
- scripted native LLM calls `web_search`.
- resulting tool call still appears in runtime `tool_calls`.
- assistant loop metadata records validator/executor path as it does for normal tools.
- no test calls `registry.run(...)` from skill loader or capability catalog.
- `run_skill` is absent from default registry.

## Acceptance Commands

Targeted Phase 3 gate:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase3_skill_system_gate.py tests/test_skill_loader.py tests/test_tool_catalog.py tests/test_assistant_context_renderer.py -q
```

Tool governance regression:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_tool_governance_contracts.py tests/test_tool_executor.py tests/test_native_tool_call_handoff.py tests/test_architecture_boundaries.py -q
```

Fast suite:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Diff check:

```bash
git diff --check -- AGENTS.md docs src tests skills
```

## Stop Criteria

Phase 3 v1 is complete when:

- A repo-local skill can describe a capability backed by governed tools.
- Disabled, manual-only, invalid and under-permissioned local skills are omitted from prompt context.
- Disabled or invalid local skill ids suppress same-name built-in fallback.
- Skill exposure is visible through a prompt-safe report.
- Unknown permission vocabulary cannot leak into prompt context.
- LLM/native tool call path proves skill guidance does not bypass `ActionValidator -> ToolExecutor -> ToolRegistry`.
- No new skill runtime, marketplace, workflow engine or memory schema is introduced.

## Implementation Shape

Expected implementation is small and conservative:

- Extend current loader validation for permission vocabulary.
- Track repo-local skill ids from both valid descriptors and load issues.
- Adjust built-in fallback selection so local issues suppress same-name built-ins.
- Add a structured skill exposure report to the existing context/capability schema.
- Thread that report through context pack or context report without changing runtime ownership.
- Add tests listed above.
- Update `docs/context_engineering_status.md`, `docs/tool-calling-architecture.md` and `docs/roadmaps/personal-realtime-ai-assistant-roadmap.md` only where current behavior changes.

Do not modify Gateway, Realtime orchestration, MemoryManager, ToolExecutor behavior or provider adapters unless a contract test proves Phase 3 cannot be validated without a narrow change.
