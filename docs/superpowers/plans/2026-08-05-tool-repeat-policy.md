# Tool 重复调用策略 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 ToolSpec 声明统一控制单个 Tool 在一次 run 内能否使用不同参数重复调用，并解除购物工具的硬编码单次限制。

**Architecture:** `ToolSpec.repeat_policy` 是唯一的 Tool 级重复策略事实源；Registry 从 Tool 声明投影，assistant loop guard 在执行前读取它。全局 `max_tool_iterations` 和相同输入去重继续独立生效。

**Tech Stack:** Python 3.12、Pydantic、LangGraph assistant loop、pytest。

## Global Constraints

- 默认 `repeat_policy=once_per_run`。
- `distinct_inputs` 只允许不同规范化输入重复执行；相同成功输入继续复用已有结果。
- `shopping_search` 显式声明 `distinct_inputs`。
- 所有策略都受 `max_tool_iterations` 限制。
- 不新增真实 Provider 调用，不修改 `tests/core`。

---

### Task 1: 建立 ToolSpec 重复策略契约

**Files:**
- Modify: `src/assistant_agent/tools/models.py`
- Modify: `src/assistant_agent/tools/base.py`
- Modify: `src/assistant_agent/tools/registry.py`
- Test: `tests/tdd/tool-repeat-policy/test_tool_repeat_policy.py`

**Interfaces:**
- Produces: `ToolRepeatPolicy = Literal["once_per_run", "distinct_inputs"]`
- Produces: `ToolSpec.repeat_policy: ToolRepeatPolicy`
- Consumes: Tool 实现的类属性 `repeat_policy`

- [ ] **Step 1: 写失败测试**

  构造默认 Probe Tool 与声明 `distinct_inputs` 的 Probe Tool，断言 Registry ToolSpec 分别返回
  `once_per_run` 和 `distinct_inputs`。

- [ ] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/tool-repeat-policy/test_tool_repeat_policy.py`

  Expected: 因 `ToolSpec` 尚无 `repeat_policy` 而失败。

- [ ] **Step 3: 最小实现契约**

  在 models 定义 Literal；Tool Protocol/ToolBase 增加默认类属性；Registry `_declared_contract()` 投影该字段。

- [ ] **Step 4: 运行 GREEN**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/tool-repeat-policy/test_tool_repeat_policy.py`

  Expected: PASS。

### Task 2: 用声明式策略替换 Runtime 硬编码

**Files:**
- Modify: `src/assistant_agent/runtime/loop_guard.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/tool.py`
- Test: `tests/tdd/tool-repeat-policy/test_tool_repeat_policy.py`

**Interfaces:**
- Consumes: `ToolSpec.repeat_policy`
- Produces: guard reason code `tool_repeat_limit_reached`

- [ ] **Step 1: 写 Runtime RED 测试**

  覆盖默认 Tool 不同参数第二次被阻止、`distinct_inputs` 不同参数第二次允许、相同成功参数仍去重、
  shopping ToolSpec opt-in 以及全局预算优先。

- [ ] **Step 2: 运行 RED 并确认失败原因**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/tool-repeat-policy`

  Expected: Runtime 仍只硬编码 shopping，且没有通用策略。

- [ ] **Step 3: 实现最小 Runtime guard**

  记录成功 Tool 名；在 `_apply_decision_guards()` 从 Registry 读取 ToolSpec；删除 shopping 专用判断；
  在执行节点为 `tool_repeat_limit_reached` 产生结构化 rejected observation；shopping Tool 声明
  `repeat_policy="distinct_inputs"`。

- [ ] **Step 4: 运行 GREEN 与现有购物回归**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/tool-repeat-policy tests/tdd/shopping-detail-runtime-projection/test_shopping_run_limit.py`

  Expected: 新策略测试 PASS；旧购物专用测试按新契约更新后 PASS。

### Task 3: 文档、静态检查与回归

**Files:**
- Modify: `docs/tool-calling-architecture.md`
- Test: `tests/tdd/tool-repeat-policy/test_tool_repeat_policy.py`

**Interfaces:**
- Documents: Tool repeat policy、全局预算、相同输入去重的优先关系。

- [ ] **Step 1: 同步权威文档**

  在 ToolSpec 契约与失败治理章节记录 `repeat_policy`，删除购物工具硬编码单次限制的描述（若存在）。

- [ ] **Step 2: 执行定向测试和 Ruff**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/tool-repeat-policy tests/tdd/shopping-detail-runtime-projection`

  Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check src/assistant_agent/tools/models.py src/assistant_agent/tools/base.py src/assistant_agent/tools/registry.py src/assistant_agent/runtime/loop_guard.py src/assistant_agent/runtime/assistant_loop_nodes.py src/assistant_agent/tools/plugins/builtin/shopping/tool.py tests/tdd/tool-repeat-policy`

  Expected: 全部通过。

- [ ] **Step 3: 执行离线 core suite**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q`

  Expected: PASS，且无真实 Provider 调用。

- [ ] **Step 4: 原子提交**

  只暂存本任务 hunk，保留工作区中其他用户改动；提交信息使用
  `feat(runtime): add per-tool repeat policy`。
