# 渐进式 Skill CapabilityGrant 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让领域 Skill 在激活后才向模型暴露 Tool schema，并让激活结果在同一 session 内持续恢复。

**Architecture:** `skill.toml` 提供机器契约，`SKILL.md` 只提供程序性正文；`load_skill` 或结构化上下文产生统一 `CapabilityGrant`。Context 先计算 entry/media/env 资格上限，再投影 baseline 与 active grants，Runtime 负责成功加载后的即时激活和 SessionStore 持久化。

**Tech Stack:** Python 3.11、Pydantic、tomllib、LangGraph、pytest。

## Global Constraints

- 默认和测试只使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Provider。
- 不使用关键词、正则或手写用户请求规则推断意图。
- 所有显式 Tool 调用继续经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- 不实现 `tool_search`、TTL、清除、租约或自动失效。
- `docs/superpowers/**` 为开发材料，默认不纳入最终提交。

---

### Task 1: 锁定新的 Skill 文件契约

**Files:**
- Modify: `src/assistant_agent/skills/loading.py`
- Test: `tests/tdd/travel_tool_orchestration/test_capability_grants.py`

**Interfaces:**
- Produces: `SkillDescriptor.activation: Literal["model", "context"]`
- Produces: `SkillDescriptor.discoverable: bool`
- Produces: `load_repo_skill_descriptors(root: Path) -> SkillCatalog`

- [ ] **Step 1: 写 RED 测试**

测试创建只有 `skill.toml` 与纯 Markdown `SKILL.md` 的临时 Skill，断言 loader 返回 descriptor、Tool grants 和正文；再用缺失 manifest、ID 不匹配、机器章节、越界 reference 断言结构化 issue。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel_tool_orchestration/test_capability_grants.py -k loader
```

预期因当前 loader 仍要求 SKILL.md frontmatter 和机器章节而失败。

- [ ] **Step 3: 最小实现**

使用 `tomllib` 解析 schema v1；Pydantic 校验 ID、版本、activation 和非重复 Tool；保留 reference 的 realpath/symlink 防护；不再从 Markdown 解析机器字段。

- [ ] **Step 4: 运行 GREEN**

运行 Step 2 同一命令，预期通过。

### Task 2: 建立通用 Grant 与 Session 持久化

**Files:**
- Create: `src/assistant_agent/runtime/capability_grants.py`
- Modify: `src/assistant_agent/runtime/session_models.py`
- Modify: `src/assistant_agent/runtime/session_store.py`
- Modify: `src/assistant_agent/runtime/state.py`
- Test: `tests/tdd/travel_tool_orchestration/test_capability_grants.py`

**Interfaces:**
- Produces: `CapabilityGrant(source, grant_id, agent_id, skill_id, tool_names)`
- Produces: `SessionStore.grant_capability(user_id, session_id, grant) -> SessionRecord`
- Produces: `CapabilityGrantController.prepare_run(state, tool_specs)`
- Produces: `CapabilityGrantController.handle_tool_result(state, result)`

- [ ] **Step 1: 写 RED 测试**

断言 SessionStore 按 owner 隔离、同 `grant_id` 幂等替换；断言 controller 只接受成功且可信的 model Skill 加载结果，并从当前 manifest 重建历史 Tool 名单。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel_tool_orchestration/test_capability_grants.py -k 'grant or session'
```

预期因 `CapabilityGrant` 和 store API 不存在而失败。

- [ ] **Step 3: 最小实现**

新增 Pydantic Grant；为内存与 JSONL store 增加 owner-scoped upsert；在 AgentState/SessionRecord 保存 grants；controller 处理恢复、context activation 和成功 `load_skill`。

- [ ] **Step 4: 运行 GREEN**

运行 Step 2 同一命令，预期通过。

### Task 3: 让 catalog 真正渐进暴露

**Files:**
- Modify: `src/assistant_agent/context/tool_catalog.py`
- Modify: `src/assistant_agent/context/builder.py`
- Modify: `src/assistant_agent/context/models.py`
- Modify: `src/assistant_agent/context/observability.py`
- Modify: `src/assistant_agent/runtime/system_prompt_policy.py`
- Test: `tests/tdd/travel_tool_orchestration/test_capability_grants.py`

**Interfaces:**
- Consumes: `AgentState.capability_grants`
- Produces: `ToolCatalogSelection.discoverable_skill_ids`
- Produces: `ToolCatalogSelection.skill_granted_tool_names`

- [ ] **Step 1: 写 RED 测试**

断言无 Grant 时 `lodging_search` 不在 `RunToolCatalog` 但旅行 card 存在；加入 Grant 后旅行 Tool 与完整正文出现；entry allowlist 仍能排除已授予 Tool；伪造 `metadata.tool_visibility.enabled_skills` 无效。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel_tool_orchestration/test_capability_grants.py -k catalog
```

预期当前默认激活实现使断言失败。

- [ ] **Step 3: 最小实现**

资格检查保持不变；普通 run 只投影未被 Skill claim 的兼容 baseline、控制工具与 active grants；workflow/durable 的可信 allowlist 继续直达。Builder 把 discoverable model Skill 渲染成 card，把 active Skill 渲染成完整正文。

- [ ] **Step 4: 运行 GREEN**

运行 Step 2 同一命令，预期通过。

### Task 4: 接入 assistant loop 并验证跨 turn

**Files:**
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Test: `tests/tdd/travel_tool_orchestration/test_capability_grants.py`

**Interfaces:**
- Consumes: `GraphRuntimeContext.tool_result_handler`
- Produces: 成功 `load_skill` 后下一次 Provider 请求立即扩大 Tool catalog

- [ ] **Step 1: 写 RED 集成测试**

用 scripted adapter 依次返回 `load_skill`、领域 Tool、最终文本；断言第一请求不含领域 Tool、第二请求含领域 Tool。复用 SessionStore 发起第二个 run，断言第一次请求已恢复领域 Tool 且无需再次加载。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel_tool_orchestration/test_capability_grants.py -k runtime
```

预期因 assistant loop 尚未处理 Grant 而失败。

- [ ] **Step 3: 最小实现**

Graph runtime 以非 checkpoint dependency 注入通用 result handler；Runtime 创建 controller，在 state 创建后恢复 grants，并在成功 ToolResult 后持久化；持久化错误记录结构化 AgentError 但不撤销当前 run Grant。

- [ ] **Step 4: 运行 GREEN**

运行 Step 2 同一命令，预期通过。

### Task 5: 迁移领域 Skill

**Files:**
- Create: `skills/travel-tool-orchestration/skill.toml`
- Modify: `skills/travel-tool-orchestration/SKILL.md`
- Create: `skills/workspace-communications/skill.toml`
- Create: `skills/workspace-communications/SKILL.md`
- Create: `skills/visual-creation/skill.toml`
- Create: `skills/visual-creation/SKILL.md`
- Create: `skills/visual-context/skill.toml`
- Create: `skills/visual-context/SKILL.md`
- Test: `tests/tdd/travel_tool_orchestration/test_capability_grants.py`

**Interfaces:**
- `travel-tool-orchestration`, `workspace-communications`, `visual-creation`: `activation="model"`
- `visual-context`: `activation="context"`

- [ ] **Step 1: 写/保留 RED 断言**

断言仓库 Skill 均能加载；旅行/工作区/视觉创作在无 Grant 时只出现 card；附图请求自动激活 `visual-context`，无媒体请求不激活。

- [ ] **Step 2: 迁移最小正文与 manifest**

旅行正文删除 frontmatter 和机器章节；新增三个简洁 Skill，正文只描述组合流程和事实边界，不复制 schema。

- [ ] **Step 3: 运行 GREEN**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel_tool_orchestration
```

### Task 6: 同步权威并完成验证

**Files:**
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/context_engineering_status.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/gateway-architecture.md`（仅 SessionRecord 边界需要时）

**Interfaces:**
- Documents: `CapabilityGrant`、两阶段 catalog、Session 恢复与 no-tool-search 决策

- [ ] **Step 1: 同步当前 authority**

只修改各 owner contract 覆盖的章节，不把历史 spec 当成当前权威。

- [ ] **Step 2: 运行 feature 与 authority 验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel_tool_orchestration
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
git diff --check
```

- [ ] **Step 3: 复核并提交**

只提交源码、Skill、TDD 测试和当前 authority；保留 `docs/superpowers/specs/**`、`docs/superpowers/plans/**` 为未跟踪开发材料。
