# 工具元数据清理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保持“注册即暴露、媒体按结构化条件过滤”的现状，删除不生效的默认暴露元数据，同时保留 `category` 的执行语义并把 Python 工具归类为 `write`。

**Architecture:** `ToolSpec.category` 继续供重试、取消、durable、realtime 和审计读取。删除 `enabled_by_default`、MCP `enabled_tools` 以及 `host_configured_tool_names` 从 Tool、Registry、Context、Runtime 和配置模型中的全部传递，避免产生不存在的暴露含义。

**Tech Stack:** Python 3.11、Pydantic、pytest、项目 Tool Registry / MCP adapter / Context catalog。

## Global Constraints

- 所有成功注册且满足媒体条件的工具默认暴露；入口 `allowed_tools` 仍可收窄目录。
- `category` 不控制暴露，只控制执行、副作用恢复和审计语义。
- pytest 使用 mock/fake，不调用真实 Provider 或 MCP。
- 不回滚工作区已有改动，不提交本计划文档。

---

### Task 1: 删除 Tool 默认暴露元数据

**Files:**
- Modify: `src/assistant_agent/tools/models.py`
- Modify: `src/assistant_agent/tools/base.py`
- Modify: `src/assistant_agent/tools/decorators.py`
- Modify: `src/assistant_agent/tools/registry.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/python_execution/tool.py`
- Test: `tests/contract/tools/test_tool_governance.py`
- Test: `tests/integration/tools/test_tool_plugin_l2.py`

**Interfaces:**
- Consumes: 现有 `ToolSpec.category` 和 Tool 类静态契约。
- Produces: 不含 `enabled_by_default` 的 `ToolSpec`，以及 `category="write"` 的 `python_interpreter`。

- [x] **Step 1: 修改契约测试，断言 ToolSpec 不再含 `enabled_by_default`，Python 分类为 `write`**
- [x] **Step 2: 运行定向测试，确认因旧字段仍存在、Python 分类仍为 dangerous 而失败**
- [x] **Step 3: 从 ToolSpec、ToolBase、decorator 和 Registry contract builder 删除字段并修改 Python 分类**
- [x] **Step 4: 重跑定向测试并确认通过**

### Task 2: 删除无效 host-configured 暴露参数链

**Files:**
- Modify: `src/assistant_agent/tools/registry.py`
- Modify: `src/assistant_agent/context/tool_catalog.py`
- Modify: `src/assistant_agent/context/service.py`
- Modify: `src/assistant_agent/context/builder.py`
- Modify: `src/assistant_agent/context/observability.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Test: `tests/integration/tools/test_tool_plugin_l2.py`
- Test: `tests/integration/context/`

**Interfaces:**
- Consumes: 当前只依赖 Registry、媒体事实和入口 `allowed_tools` 的目录选择。
- Produces: 不再接收或计算 `host_configured_tool_names` 的 Context/Runtime API。

- [x] **Step 1: 修改测试与调用样例，移除 host-configured 参数并守住“所有结构合格工具均暴露”**
- [x] **Step 2: 运行定向测试，确认旧函数签名造成失败**
- [x] **Step 3: 删除 Registry 方法、Context 参数和 assistant loop helper**
- [x] **Step 4: 运行工具与 Context 定向测试**

### Task 3: 删除 MCP enabled_tools 配置

**Files:**
- Modify: `src/assistant_agent/mcp/config.py`
- Modify: `src/assistant_agent/mcp/adapter.py`
- Modify: `deploy/mcp_servers.example.json`
- Modify: `.local/mcp_servers.json`
- Modify: `evals/agent/travel_support.py`
- Test: `tests/contract/tools/test_mcp_config_examples.py`
- Test: `tests/integration/tools/test_mcp_runtime_registration.py`
- Test: `tests/integration/tools/test_mcp_sdk_environment.py`

**Interfaces:**
- Consumes: MCP `allowed_tools` 与 `read_only_tools`。
- Produces: allowlist 内发现成功的 MCP Tool 全部注册；分类仍由 `read_only_tools` 决定。

- [x] **Step 1: 修改 MCP 配置契约测试，断言公开配置不含 `enabled_tools`**
- [x] **Step 2: 运行测试，确认旧配置模型与示例导致失败**
- [x] **Step 3: 删除配置字段、adapter 投影和所有配置文件中的键**
- [x] **Step 4: 重跑 MCP 契约与注册测试**

### Task 4: 同步架构说明并完成阶段验证

**Files:**
- Modify: `docs/tool-calling-architecture.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes: 前三项完成后的实际 Tool 契约。
- Produces: “注册即暴露、媒体条件过滤、category 仅用于执行语义”的一致文档。

- [x] **Step 1: 删除 `enabled_by_default`、`enabled_tools` 和 host-configured 暴露说明**
- [x] **Step 2: 记录 Python 为 write 以及 MCP allowlist 全量注册语义**
- [x] **Step 3: 运行最小充分 pytest、`compileall` 与 `git diff --check`**
- [x] **Step 4: 审查已知无关失败和工作区提交边界**
