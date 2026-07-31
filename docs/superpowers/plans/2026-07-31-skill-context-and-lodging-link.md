# Skill Context 与住宿链接交付实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让项目 Skill 以独立、渐进且 Provider-aware 的上下文层工作，消除重复提示和内部动作播报，并确保住宿工具提供链接时回答能够交付链接。

**Architecture:** `ContextSection` 继续作为 Provider-neutral 的 Skill 权威层；`load_skill` 的 ToolResult 只充当受治理加载回执，正文从注册源重建。编译时根据 `ProviderCapabilities.supports_developer_role` 把 Skill guidance 放入 `developer` role，否则并入 `system`；已加载正文替换同 Skill 的摘要。住宿链接继续作为 `lodging_search` 结构化证据，由 Skill 回答契约和 Agent eval 约束模型输出。

**Tech Stack:** Python 3.11、Pydantic、OpenAI-compatible Chat Completions、pytest、assistant_agent Agent eval。

## Global Constraints

- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，本任务不调用真实 Provider。
- ToolResult 仍是证据数据；Skill 正文不得直接信任 tool message 原文。
- `qwen`、`deepseek`、`ark`、`local` 和 `mock` 保守声明不支持 `developer` role。
- 不修改或回滚当前工作树中的 observability 用户改动。
- Core invariant unchanged；RED/GREEN 只放在 `tests/tdd/travel_tool_orchestration/`。

---

### Task 1: Skill Context 生命周期与 role 编译

**Files:**
- Modify: `src/assistant_agent/providers/specs.py`
- Modify: `src/assistant_agent/context/builder.py`
- Modify: `src/assistant_agent/context/prompt_compiler.py`
- Modify: `src/assistant_agent/context/service.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `tests/tdd/travel_tool_orchestration/test_progressive_skill_loading.py`
- Modify: `tests/tdd/travel_tool_orchestration/test_project_skill_context.py`

**Interfaces:**
- Consumes: `ContextSection(kind=skill_summary|skill_body|skill_reference, authority=procedural_guidance)`。
- Produces: `ProviderCapabilities.supports_developer_role: bool`；`PromptCompileRequest.supports_developer_role: bool`；加载后的同名 `skill_body` 替换 `skill_summary`。

- [ ] **Step 1: 写失败测试**

  增加以下可观察断言：

  ```python
  assert "skill_summary" not in loaded_kinds
  assert loaded_kinds.count("skill_body") == 1
  assert compiled.chat_request.messages[1] == {
      "role": "developer",
      "content": body.content,
  }
  ```

  对不支持 `developer` 的请求断言仍只有首条 `system` 承载 Skill guidance。

- [ ] **Step 2: 运行 RED**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock \
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
    tests/tdd/travel_tool_orchestration/test_progressive_skill_loading.py \
    tests/tdd/travel_tool_orchestration/test_project_skill_context.py
  ```

  预期：摘要仍与正文共存，且编译请求不支持 developer role。

- [ ] **Step 3: 最小实现**

  - 从成功的 `load_skill` observation 解析已加载 Skill ID；
  - 自动摘要生成时跳过已加载 ID；
  - 为 Provider capability、ContextService 和 PromptCompileRequest 增加保守的 role capability；
  - `supports_developer_role=true` 时从 system 中移除 Skill guidance，并插入独立 developer message；
  - `answer_only=true` 时仍不注入 Skill guidance。

- [ ] **Step 4: 运行 GREEN**

  重复 Step 2 命令，预期全部通过。

### Task 2: Skill 文案与内部动作静默

**Files:**
- Modify: `src/assistant_agent/skills/loading.py`
- Modify: `src/assistant_agent/runtime/system_prompt_policy.py`
- Modify: `skills/travel-tool-orchestration/SKILL.md`

**Interfaces:**
- Consumes: Skill descriptor 的 `name`、`description`、`activation_summary`。
- Produces: 简洁中文内部工作流摘要；工具调用不生成面向用户的 Skill/工具加载播报；住宿链接交付契约。

- [ ] **Step 1: 使用已有失败 Trace 作为 RED 证据**

  Trace `3e35ec41fd2d97a283d2ac4ec2279e4f` 已证明当前模型输出“我先读取 Skill”，且 L0/L1 标题和英文 description 重复。

- [ ] **Step 2: 最小修改**

  - 将 `# 可用项目 Skill` / `# 项目 Skill` 改为面向模型的“内部工作流”表达；
  - activation summary 只说明工作流 ID、最小路由和内部加载约束；
  - system policy 使用正向协议要求：产生 tool call 的 assistant message 只承载调用，不承载用户可见进度；
  - Skill description 改为中文触发条件；
  - lodging offer 有 `booking_url` 时输出可点击 OTA 跳转链接；没有时明确无链接，不生成悬空“点击链接”。

- [ ] **Step 3: 运行 Skill loader 与 TDD 验证**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock \
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
    tests/tdd/travel_tool_orchestration
  ```

### Task 3: 住宿链接 Agent eval 覆盖

**Files:**
- Modify: `evals/agent/tasks/travel_lodging_constraint_grounding/grader.py`
- Modify: `evals/agent/tasks/travel_lodging_constraint_grounding/calibration.json`

**Interfaces:**
- Consumes: `lodging_search.model_observation.offers[*].booking_url`。
- Produces: response-quality rubric 对可用链接交付和悬空链接的判断。

- [ ] **Step 1: 更新校准 Evidence**

  给正确样本的三个 offer 增加固定 `booking_url`，并在回答中逐个提供 Markdown 链接；保留负样本，使遗漏链接或悬空点击提示不能通过新增条件。

- [ ] **Step 2: 更新 rubric**

  增加：工具返回 `booking_url` 时候选必须携带对应 OTA 跳转链接；不得把跳转链接称为已预订或保证成交；没有 URL 时不得建议点击不存在的链接。

- [ ] **Step 3: 离线 inspect/calibrate**

  ```bash
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_agent_evals.py \
    --inspect --task travel_lodging_constraint_grounding
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_agent_evals.py \
    --calibrate --task travel_lodging_constraint_grounding
  ```

### Task 4: 权威文档与完成审计

**Files:**
- Modify: `docs/CONTEXT_ENGINEERING_STATUS.md`
- Modify: `docs/tool-calling-architecture.md`

**Interfaces:**
- Produces: 当前 Skill Context 生命周期、developer role capability 回退和住宿链接语义的权威说明。

- [ ] **Step 1: 同步文档**

  记录 L0 被 L1 替换、ToolResult 只作加载回执、Provider role capability 映射以及 `booking_url` 的条件性交付。

- [ ] **Step 2: 运行最小充分验证**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock \
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
    tests/tdd/travel_tool_orchestration
  ```

  并执行 Task 3 的 inspect/calibrate。

- [ ] **Step 3: 审计与提交**

  用 `git diff --check`、`git status --short` 和定向 diff 确认未包含用户 observability 改动，只提交本计划涉及的源码、Skill、eval 和权威文档；计划文档按项目规则不纳入提交。
