# 全部注册工具默认暴露实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 所有已注册且满足结构化运行条件的工具默认进入本轮 Tool catalog，并在模型选择后沿现有治理链直接执行。

**Architecture:** `ToolRegistry` 继续决定部署期工具集合，`RunToolCatalog` 继续作为模型可见与执行可用的唯一 run-scoped 集合。`category` 和 `enabled_by_default` 不再扩大或收窄默认暴露；入口 `allowed_tools`、媒体条件和 durable ready-step 约束仍可收窄集合。执行继续经过 `ActionValidator -> ToolExecutor -> ToolRegistry`，不增加确认或授权状态。

**Tech Stack:** Python 3.11、Pydantic、pytest。

## Global Constraints

- 不根据用户自然语言、关键词或正则选择工具。
- 不绕过 Provider readiness、MCP allowlist、媒体要求、durable 模式、输入 schema 或工具安全校验。
- `read` 工具仍可自动重试；`write` 与 `dangerous` 仍不自动重试，并保留既有副作用失败处理。
- 不引入用户确认、授权持久化或 Gateway 新协议。

---

### Task 1: 统一 Tool catalog 默认暴露规则

**Files:**
- Modify: `tests/integration/tools/test_tool_plugin_l2.py`
- Modify: `src/assistant_agent/context/tool_exposure.py`
- Modify: `src/assistant_agent/context/tool_catalog.py`

**Interfaces:**
- Consumes: `ToolSpec.category`、`ToolSpec.enabled_by_default`、`UserRequest.metadata.tool_visibility.allowed_tools` 和媒体事实。
- Produces: `select_prompt_tool_specs(...) -> ToolCatalogSelection`，其中所有满足结构化运行条件的已注册工具都进入 `run_tool_catalog.available_tool_names`。

- [x] **Step 1: 修改回归测试**

  在配置插件中加入 `read`、`generate`、`write`、`dangerous` 工具，并断言没有显式 opt-in 时四类工具都进入 catalog；同时保留媒体不可用和 `allowed_tools` 收窄断言。

- [x] **Step 2: 运行测试并确认 RED**

  Run:

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock \
    /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
    tests/integration/tools/test_tool_plugin_l2.py
  ```

  Expected: FAIL，现有策略仍排除未显式启用的 `write` / `dangerous` 工具。

- [x] **Step 3: 实现统一暴露**

  让 `evaluate_tool_exposure()` 在媒体条件通过后对所有 category 返回 exposed；删除 catalog 中基于 `enabled_by_default`、host configured tool 和显式 tool opt-in 的放宽逻辑。保留 `allowed_tools`、Skill 激活记录、media 和 durable filtering。

- [x] **Step 4: 运行定向测试并确认 GREEN**

  Run:

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock \
    /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
    tests/integration/tools/test_tool_plugin_l2.py \
    tests/contract/tools/test_tool_governance.py
  ```

  Expected: PASS。

### Task 2: 同步架构文档并验证故障域

**Files:**
- Modify: `docs/tool-calling-architecture.md`
- Modify: `tests/integration/eval/test_agent_eval_task.py`（仅在现有 catalog 断言需要同步时）

**Interfaces:**
- Consumes: Task 1 的统一暴露行为。
- Produces: 当前权威文档与源码一致。

- [x] **Step 1: 更新权威文档**

  删除 `write` / `dangerous` 默认隐藏和显式 opt-in 的描述，明确全部已注册工具默认暴露并直接执行；记录仍保留的结构化收窄条件和 category 执行语义。

- [x] **Step 2: 运行最小充分验证**

  Run:

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock \
    /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
    tests/contract/tools \
    tests/integration/tools \
    tests/integration/eval/test_agent_eval_task.py::test_every_agent_task_environment_exposes_complete_default_amap_catalog
  ```

  Expected: PASS。

- [x] **Step 3: 执行静态检查**

  Run:

  ```bash
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
    src/assistant_agent/context/tool_exposure.py \
    src/assistant_agent/context/tool_catalog.py \
    tests/integration/tools/test_tool_plugin_l2.py
  git diff --check
  ```

  Expected: PASS。
