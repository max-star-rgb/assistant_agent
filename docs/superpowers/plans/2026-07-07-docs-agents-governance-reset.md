# Docs And AGENTS Governance Reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reset repository docs so future development starts from a small set of current authority documents and does not route through stale plans.

**Architecture:** Keep `AGENTS.md` as the coding-agent rule entry, make `README.md` a lightweight human navigation page, preserve current authority docs and operational runbooks, and directly delete obsolete development plans plus one-off `docs/superpowers/**` artifacts after cleanup is complete. All routing changes are Markdown-only and must not touch runtime code.

**Tech Stack:** Markdown, `rg`, `find`, `git diff --check`, git tracked-file deletion.

## Global Constraints

- `AGENTS.md` remains the single coding-agent entrypoint.
- `README.md` becomes a lightweight human navigation page.
- `docs/` keeps only current architecture authorities, necessary walkthroughs, current API/runbook material, and interview material.
- Clearly stale development plans, one-off execution artifacts, and broken-link documents are deleted directly instead of preserved indefinitely.
- Delete obviously stale docs directly.
- Preserve only docs that still guide current development, operations, interviews, or project-owner understanding.
- Fix current docs when they contain stale links to removed material.
- Do not keep historical plans as active design input.
- Do not rewrite project architecture.
- Do not change runtime behavior.
- Do not rename the Python package, conda environment, or source tree.
- Do not convert README into a full marketing or public product page.
- Do not preserve every historical plan merely for record keeping.
- Protect unrelated dirty worktree changes. Before each task, run `git status --short` and only stage files listed in that task.

---

## File Structure

**Keep and edit:**

- `AGENTS.md`: concise coding-agent entrypoint, current architecture/safety/routing rules only.
- `README.md`: lightweight human navigation and local check entrypoint.
- `docs/gateway-architecture.md`: remove stale references to deleted realtime development plans while preserving Gateway authority and current entry-adapter language.
- `docs/memory-service-architecture.md`: remove routing to deleted memory hardening plan; keep memory architecture and operational runbook links.
- `docs/CONTEXT_ENGINEERING_STATUS.md`: remove routing to deleted completed context development plan.
- `docs/memory_server_api_spec.md`: replace missing `CURRENT_DESIGN` and `KNOWN_ISSUES` links with current retained docs.
- `docs/development/memory-sqlite-operator-runbook.md`: remove link to deleted memory hardening plan.
- `.codex/skills/assistant-agent-memory-service/SKILL.md`: remove routing to deleted memory hardening plan.
- `.codex/skills/assistant-runtime-reference/SKILL.md`: remove validation references to deleted Gateway development plan.

**Keep unchanged unless verification exposes stale links:**

- `docs/tool-calling-architecture.md`
- `docs/observability-harness.md`
- `docs/agent-communication-routing.md`
- `docs/context-engineering-walkthrough.md`
- `docs/memory-module-walkthrough.md`
- `docs/agent-collaboration-walkthrough.md`
- `docs/interview/**`
- `docs/development/agent-pilot-operator-runbook.md`

**Delete:**

- `docs/development/agent-control-plane-plan.md`
- `docs/development/agent-production-auth-observability-plan.md`
- `docs/development/context-engine-memory-policy-plan.md`
- `docs/development/gateway-entry-layer-development-plan.md`
- `docs/development/memory-kernel-hardening-plan.md`
- `docs/development/memory-server-integration-plan.md`
- `docs/development/realtime-agent-interrupt-phase2-plan.md`
- `docs/development/realtime-agent-task-state-plan.md`
- `docs/development/realtime-call-agent-mvp-plan.md`
- `docs/development/realtime-harness-hardening-plan.md`
- `docs/development/realtime_phone_backend_plan.md`
- `docs/superpowers/**`, including this implementation plan and the governance design spec, as the final cleanup task.

---

### Task 1: Rewrite Entrypoints

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: approved governance spec in `docs/superpowers/specs/2026-07-07-docs-agents-governance-design.md`.
- Produces: repository-level routing rules used by every later docs cleanup task.

- [ ] **Step 1: Inspect current dirty worktree**

Run:

```bash
git status --short
```

Expected: may show unrelated modified source/test files. Do not stage source/test files in this task.

- [ ] **Step 2: Replace `AGENTS.md` with concise current rules**

Use `apply_patch` to replace the full file with:

```markdown
# AGENTS.md

本文件是 Codex / coding agent 的仓库级入口。开始仓库内任何非纯问答、非单条无副作用命令任务前，以本文件为准。README 面向人类快速导航；专项架构细节保留在少量 `docs/` 权威文档中。

## 1. 项目定位

本仓库项目名、展示名、发行名和 Python 包名均为 `assistant_agent`，包目录为 `src/assistant_agent/`。本地 conda 环境仍为 `hello_agent`，除非用户明确要求，不要重命名环境路径。

项目实现一个本地优先的多模态自主工具调用 Agent。Agent 负责理解用户输入、选择工具、执行受控调用、融合结果并给出最终回答；具体能力由工具、provider adapter、memory service、demo/eval/API 层协作提供。

当前核心运行时以 LangGraph/ReAct assistant loop 为主，同时保留 mock/local/offline 路径用于稳定测试和演示。真实外部 Provider 必须通过 `provider_smoke` 或 `pilot` profile 和本机未跟踪配置显式启用。

## 2. 当前权威入口

按任务范围优先使用项目内 `.codex/skills/**`。skill 是执行工作流包装，负责按需路由到对应权威 docs、源码和测试；AGENTS 不重复列出每个 skill 内部的补充阅读清单。

| scope | entry |
| --- | --- |
| Gateway、realtime frame、session/run/cancel/interrupt、WebSocket bridge、旧 `runTime` 参考边界 | `.codex/skills/assistant-runtime-reference`，权威文档是 `docs/gateway-architecture.md` |
| tool calling、ToolSpec、ActionValidator、ToolExecutor、ToolRegistry、MCP `tool_run`、工具 observation/retry/budget | `.codex/skills/assistant-agent-tool-calling`，权威文档是 `docs/tool-calling-architecture.md` |
| 记忆服务、MemoryManager、Memory Kernel、memory store/retrieval/write policy、memory API、user profile、audit、retention | `.codex/skills/assistant-agent-memory-service`，权威文档是 `docs/memory-service-architecture.md` |
| context engineering、prompt/context rendering、conversation history、memory context、tool observation compaction、context budget | `.codex/skills/assistant-agent-context-engineering`，权威文档是 `docs/CONTEXT_ENGINEERING_STATUS.md` |
| 多 agent、`assistant_agent.agent_routing`、AgentRouter、AgentDirectory、A2A/JSON-RPC、`delegate_to_agent`、pilot readiness | `.codex/skills/assistant-agent-collaboration`，权威文档是 `docs/agent-communication-routing.md` |
| 状态日志、trace、运行监控、ReAct 关键节点观测、redaction | `docs/observability-harness.md` |
| 面试训练、题库、回答点评、标准答案、面试文档更新 | `.codex/skills/assistant-agent-interview-trainer`，权威文档是 `docs/interview/README.md` |

`docs/development/**` 只保留仍有现实用途的操作 runbook 或用户明确点名的执行材料，不作为默认设计权威。不要把旧 roadmap 或阶段计划当成当前架构。

## 3. 当前架构边界

核心调用链：

```text
User / CLI / API / Web UI
        |
        v
FastAPI routes or local runner
        |
        v
AgentGraphRuntime / assistant loop
        |
        v
AssistantDecision -> ActionValidator -> ToolExecutor
        |
        v
ToolRegistry -> tools -> provider adapters / memory / local services
```

重要边界：

- Agent/graph 负责决策编排，不直接绕过工具治理边界调用外部能力。
- 工具调用必须经过 validator、executor、tool registry、policy/audit 相关边界。
- Provider adapter 负责真实或 mock 能力接入；默认 profile 必须是 mock/local/offline。
- Memory 行为应通过 memory service/provider 管理，不把临时状态散落到无关模块。
- Memory tools 只是 Agent 可调用适配器，不是记忆服务所有者；检索、写入策略、TTL、去重、用户画像、审计和 store 选择必须留在 `MemoryManager`、`memory/` 或 `services/memory_*`。
- 多 agent / A2A 行为应通过 `assistant_agent.agent_routing` 聚合入口、AgentRouter、agent communication service、directory、transport adapter 和工具治理边界管理。
- CLI、Web UI、App、HTTP、WebSocket 和 realtime call adapter 属于入口层；Gateway 是入口层之后的标准化消息、session/run 生命周期、cancel/interrupt、reconnect/hangup 和 stream frame 控制边界。
- `assistant_agent.realtime` / `GatewayAgentAdapter` 是 Gateway 到当前主运行时的薄适配层，不承担主大脑职责。
- 当前核心 runtime、Gateway session/run/cancel/interrupt/history 等生命周期权威实现继续以 Python `assistant_agent` 为主。新增 Web UI、BFF、vendor WebSocket adapter、Media Relay adapter、边缘入口或电话/实时媒体 SDK 适配层可以使用 TypeScript、Go、Rust 等非 Python 语言，但这些层必须保持薄入口适配器职责。
- AgentRouter 只负责内部 agent 选择、capability routing、controller/worker route 和控制面记录；面向调用方的多 agent 导入优先走 `assistant_agent.agent_routing`。
- 不要把 `runTime` 的旧 OpenClaw/Anthropic agent loop 引入本项目；当前 agent 内部执行器仍是 `AgentGraphRuntime` / assistant loop。
- API、demo、eval、CLI 应尽量复用同一套 runtime 行为，避免各自实现不一致的 Agent 逻辑。

## 4. 运行与安全规则

默认规则不可随意放宽：

- 仓库测试、eval、无 key 环境默认只允许 mock/local/offline 路径。
- 用户本机任务可以使用真实 LLM，但不自动调用真实图片、视频、商品、通知、数据库或其他外部 Provider。
- 不因为检测到 API key 就启用真实 Provider。
- 不写入 API key、token、真实 `.env`、真实用户数据或真实 provider raw response。
- 不提交真实媒体、生成物、大文件、缓存目录或外部服务原始返回。
- 不安装新依赖，不联网拉取依赖，除非用户明确要求并允许。

真实 Provider 调用必须同时满足：

- 用户或任务明确要求真实 Provider，或本轮任务是在用户本机真实 LLM 运行口径下执行。
- 使用受控 runtime profile，例如 `MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke` 或 `pilot`。
- key 只来自本机环境变量或用户已配置的安全位置，不能写入仓库。
- 最终报告说明调用范围和验证结果。

## 5. 本地 Python 环境

默认使用 conda 环境 `hello_agent`。Codex 执行 Python、pytest、脚本时，优先直接调用：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python
```

常用命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py --provider mock --image-provider mock
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_client.py --server http://127.0.0.1:8000 "你好"
```

只有在需要执行非 Python 命令且依赖 conda 激活环境变量时，才使用：

```bash
conda run -n hello_agent <command>
```

## 6. 目录与编辑策略

| path | responsibility | default edit policy |
| --- | --- | --- |
| `src/assistant_agent/api/` | FastAPI app、routes、server/client integration | 按任务要求修改 |
| `src/assistant_agent/gateway/` | Gateway protocol、bridge、session/run/cancel/interrupt、realtime frame lifecycle | 按任务要求修改，边界以 `docs/gateway-architecture.md` 为准 |
| `src/assistant_agent/realtime/` | Gateway 到 assistant runtime 的薄 adapter/backend contract 和 event mapping | 按任务要求修改 |
| `src/assistant_agent/agent/` | LangGraph runtime、assistant loop、决策、验证、执行 | 按任务要求修改 |
| `src/assistant_agent/services/` | runtime services、context、trace、session、agent communication、provider 管理 | 按任务要求修改 |
| `src/assistant_agent/providers/` | Provider adapter、runtime profile、mock/real 边界 | 谨慎修改，默认 mock 优先 |
| `src/assistant_agent/tools/` | Tool registry、工具实现、策略、审计 | 按任务要求修改 |
| `src/assistant_agent/memory/` | 记忆服务、检索、存储 | 按任务要求修改 |
| `src/assistant_agent/eval/` | 离线评测逻辑 | 按任务要求修改 |
| `tests/` | pytest 测试 | 修改行为时同步维护，除非用户限制只读 |
| `scripts/` | 本地验证、服务、demo、eval、smoke 脚本 | 可按任务修改 |
| `docs/` | 当前权威文档、走读文档、API/runbook、面试资料 | 文档任务优先修改 |
| `.codex/skills/` | 项目内 Codex skills，包装专项工作流并路由到权威 docs | 只放项目专用 workflow，不复制长篇架构细节 |

如果用户对本轮任务设定更严格的 scope，例如“不要修改 `src/**`”或“不要修改 `tests/**`”，以用户当前约束为准。

## 7. 编码约定

- 新代码优先放入 `src/assistant_agent/` 的既有分层。
- 公共数据结构优先使用 Pydantic model。
- 工具调用结果必须结构化，不允许只返回散乱字符串。
- 外部模型/API 先维护 adapter interface 和 mock implementation，不要直接绑定具体供应商。
- 多 agent 通信先维护内部 message/task/artifact contract 和 transport adapter；A2A JSON-RPC 只作为协议适配层，不作为核心 runtime 内部模型。
- Memory tool 代码应保持薄层，只做 `ToolContext` 身份绑定、输入适配、调用 `MemoryManager`、包装 `ToolResult`。
- Memory 工具选择采用 LLM-first：assistant loop 由 LLM 语义判断是否调用 `memory_save` / `memory_retrieval`，并在 `memory_save` 中声明 `source_intent`；任何写入仍必须经过 `MemoryWritePolicy`。
- Phase 8 之后的 assistant loop 方向是真实 LLM 自主决策、追问、工具调用和最终回答；不要让真实 LLM 路径依赖旧 intent/router/plan 来选择工具。
- mock/offline 路径只作为稳定测试与本地演示兼容层，不要把 mock 行为伪装成真实 LLM 能力。
- 新增核心 ReAct/assistant loop 测试应优先覆盖非 mock LLM 决策路径，例如 scripted/fake real chat adapter；真实外部网络调用只放在显式 opt-in 的 smoke/integration 测试中。
- 工具执行失败必须返回可解释错误，不能直接抛出未处理异常给 Agent。
- 修改行为时同步更新相关测试和文档。
- 业务功能建议从具体模块导入；只有明确包级公共入口才放进 `__init__.py` 聚合导出。

## 8. 文档维护规则

- `AGENTS.md` 是当前唯一 coding-agent 工作入口，应简短稳定，不塞入长篇历史设计。
- `README.md` 是人类轻导航入口，不承担专项架构权威职责。
- 当前架构权威文档是 `docs/gateway-architecture.md`、`docs/tool-calling-architecture.md`、`docs/observability-harness.md`、`docs/memory-service-architecture.md`、`docs/CONTEXT_ENGINEERING_STATUS.md` 和 `docs/agent-communication-routing.md`。
- 走读文档只用于解释已沉淀机制，不替代权威文档。
- `docs/development/**` 只保留仍有现实用途的操作 runbook 或用户明确点名的执行材料。
- `docs/interview/**` 是面试训练资料，普通开发任务不把它当成架构来源。
- `.codex/skills/**` 只包装项目专用工作流和入口路由，不取代对应权威 docs。
- 新增文档必须有明确长期用途；优先更新 AGENTS、README 或现有专项文档。

## 9. 测试与验收

小开发优先使用快测层和相关专项测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/unit tests/contracts -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/<relevant_test_file>.py -q
```

完整离线验证：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```

如环境安装了工具，可补充：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff format --check .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m mypy src
```

如果命令不存在或失败，不要为了让测试过而擅自改无关源码；应记录命令、结果和失败原因。

## 10. 工作模式

开始任务时：

```text
我将处理：...
我会先阅读：...
计划：...
```

执行过程中：

- 先读相关代码和文档，再做判断。
- 保持 scope 小而明确，不跨任务提前实现未来能力。
- 手工新建或修改文件默认使用 `apply_patch`。
- 使用 `rg` / `rg --files` 搜索文件和文本。
- 不回滚用户已有改动；遇到 dirty worktree 时先识别改动来源，和当前任务无关则保持不动。
- 不用 `python -c` 绕过任务 scope 写文件；它只用于受控机械替换、环境检查或小范围验证。

结束任务时：

```text
完成内容：...
修改文件：...
测试结果：...
未完成/限制：...
下一步建议：...
```

## 11. 业务专项说明

涉及好单库相关功能时，先读取：

- `haodanku-openapi-docs/AI使用说明.md`
- `haodanku-openapi-docs/接口目录.md`

再按意图路由到对应分类文档编码。不要跳过本地 validator、executor、policy、audit 边界直接调用外部服务。
```

- [ ] **Step 3: Replace `README.md` with lightweight human navigation**

Use `apply_patch` to replace the full file with:

```markdown
# assistant_agent

`assistant_agent` is a local-first multimodal autonomous tool-calling Agent. It uses a LangGraph/ReAct assistant loop, governed tool execution, provider adapters, memory services, API/demo/eval surfaces, and optional realtime Gateway entry layers.

## Start Here

- Coding-agent rules and repository boundaries: `AGENTS.md`
- Gateway and realtime lifecycle: `docs/gateway-architecture.md`
- Tool calling governance: `docs/tool-calling-architecture.md`
- Observability and trace harness: `docs/observability-harness.md`
- Memory service architecture: `docs/memory-service-architecture.md`
- Context engineering status: `docs/CONTEXT_ENGINEERING_STATUS.md`
- Multi-agent routing: `docs/agent-communication-routing.md`

Walkthroughs for project-owner explanations:

- `docs/context-engineering-walkthrough.md`
- `docs/memory-module-walkthrough.md`
- `docs/agent-collaboration-walkthrough.md`

Interview training material lives under `docs/interview/` and is separate from normal development routing.

## Local Environment

The Python package is `assistant_agent` under `src/assistant_agent/`. The local conda environment remains `hello_agent`.

Default local runs use mock/local/offline providers. Real external providers are opt-in only through explicit runtime profiles such as `provider_smoke` or `pilot`, with keys supplied by local environment/config outside the repository.

Basic checks:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Full offline validation:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```

## Documentation Policy

`AGENTS.md` is the coding-agent entrypoint. README is only a human navigation page. Current architecture decisions belong in the focused authority docs listed above. `docs/development/` is not a default starting point; it should contain only retained operational runbooks or explicitly named execution material.
```

- [ ] **Step 4: Verify entrypoint wording**

Run:

```bash
rg -n "README 暂时|占位入口|docs/development/.*暂不清理|realtime-harness-hardening-plan|memory-kernel-hardening-plan|context-engine-memory-policy-plan" AGENTS.md README.md
```

Expected: no output.

- [ ] **Step 5: Check Markdown whitespace**

Run:

```bash
git diff --check -- AGENTS.md README.md
```

Expected: no output.

- [ ] **Step 6: Commit entrypoint rewrite**

Run:

```bash
git add AGENTS.md README.md
git commit -m "docs: reset repository entrypoints"
```

Expected: commit succeeds and stages only `AGENTS.md` and `README.md`.

---

### Task 2: Delete Obsolete Development Plans

**Files:**
- Delete: `docs/development/agent-control-plane-plan.md`
- Delete: `docs/development/agent-production-auth-observability-plan.md`
- Delete: `docs/development/context-engine-memory-policy-plan.md`
- Delete: `docs/development/gateway-entry-layer-development-plan.md`
- Delete: `docs/development/memory-kernel-hardening-plan.md`
- Delete: `docs/development/memory-server-integration-plan.md`
- Delete: `docs/development/realtime-agent-interrupt-phase2-plan.md`
- Delete: `docs/development/realtime-agent-task-state-plan.md`
- Delete: `docs/development/realtime-call-agent-mvp-plan.md`
- Delete: `docs/development/realtime-harness-hardening-plan.md`
- Delete: `docs/development/realtime_phone_backend_plan.md`

**Interfaces:**
- Consumes: entrypoint policy from Task 1.
- Produces: `docs/development/` containing only retained runbooks unless a future active plan is explicitly added.

- [ ] **Step 1: Confirm retained runbooks before deleting**

Run:

```bash
find docs/development -maxdepth 1 -type f -printf '%f\n' | sort
```

Expected: list includes `agent-pilot-operator-runbook.md` and `memory-sqlite-operator-runbook.md`.

- [ ] **Step 2: Delete obsolete plans**

Run:

```bash
git rm \
  docs/development/agent-control-plane-plan.md \
  docs/development/agent-production-auth-observability-plan.md \
  docs/development/context-engine-memory-policy-plan.md \
  docs/development/gateway-entry-layer-development-plan.md \
  docs/development/memory-kernel-hardening-plan.md \
  docs/development/memory-server-integration-plan.md \
  docs/development/realtime-agent-interrupt-phase2-plan.md \
  docs/development/realtime-agent-task-state-plan.md \
  docs/development/realtime-call-agent-mvp-plan.md \
  docs/development/realtime-harness-hardening-plan.md \
  docs/development/realtime_phone_backend_plan.md
```

Expected: git reports removal of the eleven listed files.

- [ ] **Step 3: Verify development directory now contains only retained runbooks**

Run:

```bash
find docs/development -maxdepth 1 -type f -printf '%f\n' | sort
```

Expected:

```text
agent-pilot-operator-runbook.md
memory-sqlite-operator-runbook.md
```

- [ ] **Step 4: Commit obsolete plan deletion**

Run:

```bash
git add docs/development
git commit -m "docs: remove obsolete development plans"
```

Expected: commit succeeds with deletions only under `docs/development/`.

---

### Task 3: Repair References After Development Cleanup

**Files:**
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/CONTEXT_ENGINEERING_STATUS.md`
- Modify: `docs/development/memory-sqlite-operator-runbook.md`
- Modify: `.codex/skills/assistant-agent-memory-service/SKILL.md`
- Modify: `.codex/skills/assistant-runtime-reference/SKILL.md`

**Interfaces:**
- Consumes: deleted development-plan file list from Task 2.
- Produces: retained authority docs and skills with no references to deleted plans.

- [ ] **Step 1: Locate references to deleted development plans**

Run:

```bash
rg -n "docs/development/(agent-control-plane-plan|agent-production-auth-observability-plan|context-engine-memory-policy-plan|gateway-entry-layer-development-plan|memory-kernel-hardening-plan|memory-server-integration-plan|realtime-agent-interrupt-phase2-plan|realtime-agent-task-state-plan|realtime-call-agent-mvp-plan|realtime-harness-hardening-plan|realtime_phone_backend_plan)\\.md" AGENTS.md README.md docs .codex/skills -g '*.md'
```

Expected: matches in the files listed for this task. Every match must be removed or replaced in this task.

- [ ] **Step 2: Update `docs/gateway-architecture.md`**

Use `apply_patch` to make these replacements.

Replace the paragraph that mentions task-state and harness development plans with:

```markdown
Realtime task state, deterministic fallback behavior, tool-wait boundaries, and interrupt/cancel handling are part of the current Gateway lifecycle contract when implemented. Keep current behavior in this document and in tests, not in archived phase plans.
```

Replace the `Update Rules` bullet list with:

```markdown
## Update Rules

- Keep current Gateway protocol, lifecycle, adapter, and entry-layer decisions in this file.
- Keep `AGENTS.md` as the concise routing entry and this file as the Gateway-specific authority.
- Keep `.codex/skills/assistant-runtime-reference/SKILL.md` routing to this file before any legacy `runTime` reference.
- Do not put active Gateway architecture decisions only in `docs/development/**`; retained development files are runbooks or explicitly named execution material.
```

- [ ] **Step 3: Update `docs/memory-service-architecture.md`**

Use `apply_patch` to remove the sentence:

```markdown
Future engineering hardening should follow `docs/development/memory-kernel-hardening-plan.md` after reading this architecture document.
```

If the surrounding introduction needs a replacement sentence, insert:

```markdown
Future memory architecture changes should be reflected in this document first; operational SQLite procedures remain in `docs/development/memory-sqlite-operator-runbook.md`.
```

- [ ] **Step 4: Update `docs/CONTEXT_ENGINEERING_STATUS.md`**

Use `apply_patch` to replace every reference to `docs/development/context-engine-memory-policy-plan.md` with this wording:

```markdown
The completed context-engineering phase plan has been removed from the active docs set; this file is the current context-engineering status and handoff entry.
```

If a bullet becomes too long, split it into two bullets without adding a new file reference.

- [ ] **Step 5: Update `docs/development/memory-sqlite-operator-runbook.md`**

Use `apply_patch` to replace the initial references list with:

```markdown
Reference docs:

- `docs/memory-service-architecture.md`
```

- [ ] **Step 6: Update `.codex/skills/assistant-agent-memory-service/SKILL.md`**

Use `apply_patch` to replace the start checklist item that routes to the deleted hardening plan with:

```markdown
4. If the task explicitly concerns SQLite backup, restore, integrity check, or index rebuild operations, read `docs/development/memory-sqlite-operator-runbook.md` as operational guidance.
```

Keep the remaining instruction that treats other `docs/development/**` files as non-default historical material.

- [ ] **Step 7: Update `.codex/skills/assistant-runtime-reference/SKILL.md`**

Use `apply_patch` to remove `docs/development/gateway-entry-layer-development-plan.md` from the validation command. The resulting command must be:

```bash
git diff --check -- AGENTS.md docs/gateway-architecture.md .codex/skills/assistant-runtime-reference src/assistant_agent/gateway src/assistant_agent/api/gateway_runtime.py src/assistant_agent/api/gateway_websocket.py tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py scripts/run_gateway_client.py
```

- [ ] **Step 8: Verify deleted-plan references are gone**

Run:

```bash
rg -n "docs/development/(agent-control-plane-plan|agent-production-auth-observability-plan|context-engine-memory-policy-plan|gateway-entry-layer-development-plan|memory-kernel-hardening-plan|memory-server-integration-plan|realtime-agent-interrupt-phase2-plan|realtime-agent-task-state-plan|realtime-call-agent-mvp-plan|realtime-harness-hardening-plan|realtime_phone_backend_plan)\\.md" AGENTS.md README.md docs .codex/skills -g '*.md'
```

Expected: no output.

- [ ] **Step 9: Check Markdown whitespace**

Run:

```bash
git diff --check -- docs/gateway-architecture.md docs/memory-service-architecture.md docs/CONTEXT_ENGINEERING_STATUS.md docs/development/memory-sqlite-operator-runbook.md .codex/skills/assistant-agent-memory-service/SKILL.md .codex/skills/assistant-runtime-reference/SKILL.md
```

Expected: no output.

- [ ] **Step 10: Commit reference repair**

Run:

```bash
git add docs/gateway-architecture.md docs/memory-service-architecture.md docs/CONTEXT_ENGINEERING_STATUS.md docs/development/memory-sqlite-operator-runbook.md .codex/skills/assistant-agent-memory-service/SKILL.md .codex/skills/assistant-runtime-reference/SKILL.md
git commit -m "docs: repair governance references"
```

Expected: commit succeeds and stages only the files listed above.

---

### Task 4: Repair Memory Server Documentation Role

**Files:**
- Modify: `docs/memory_server_api_spec.md`
- Modify: `docs/memory_server_software_implementation_design.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: retained memory architecture and external Memory Server adapter docs.
- Produces: memory server docs that no longer point to missing `CURRENT_DESIGN` or `KNOWN_ISSUES` files and are clearly marked as external Memory Server contract/reference material.

- [ ] **Step 1: Confirm Memory Server docs still correspond to current adapter code**

Run:

```bash
rg -n "Memory Server|memory_server|memory server" src/assistant_agent tests scripts docs/memory-service-architecture.md docs/tool-calling-architecture.md
```

Expected: output includes `src/assistant_agent/memory/remote.py`, `src/assistant_agent/services/memory_media_ingestion.py`, `src/assistant_agent/tools/memory_media_tool.py`, `scripts/smoke_memory_server.py`, and related tests.

- [ ] **Step 2: Fix `docs/memory_server_api_spec.md` stale links**

Use `apply_patch` to replace the opening paragraph:

```markdown
本文档描述当前代码中的真实 HTTP contract。所有请求和响应均为 JSON；时间戳使用 ISO 8601 字符串。架构和数据层设计见 `docs/CURRENT_DESIGN.md`；部分行为仍有 known issues，见 `docs/KNOWN_ISSUES.md`。
```

with:

```markdown
本文档描述 assistant_agent 对外部 Memory Server 的当前 HTTP contract 期望。所有请求和响应均为 JSON；时间戳使用 ISO 8601 字符串。assistant_agent 侧集成边界见 `docs/memory-service-architecture.md`，外部 Memory Server 实现参考见 `docs/memory_server_software_implementation_design.md`。
```

- [ ] **Step 3: Clarify `docs/memory_server_software_implementation_design.md` status**

Use `apply_patch` to replace the status note:

```markdown
> 仅供内部使用。本文基于当前 `master` 分支实现编写，HTTP contract 以 `memory_server_api_spec.md` 为准。
```

with:

```markdown
> 仅供内部使用。本文是外部 Memory Server 的实现参考，不是 assistant_agent 的核心架构权威。assistant_agent 侧长期记忆边界以 `docs/memory-service-architecture.md` 为准；HTTP contract 以 `docs/memory_server_api_spec.md` 为准。
```

- [ ] **Step 4: Add Memory Server docs to README navigation**

Use `apply_patch` to add this block after the current architecture authority list:

```markdown
External Memory Server contract/reference material:

- `docs/memory_server_api_spec.md`
- `docs/memory_server_software_implementation_design.md`
```

- [ ] **Step 5: Verify missing-file links are gone**

Run:

```bash
rg -n "docs/(CURRENT_DESIGN|KNOWN_ISSUES|phase1-7|observability-local)" AGENTS.md README.md docs .codex/skills -g '*.md'
```

Expected: no output.

- [ ] **Step 6: Check Markdown whitespace**

Run:

```bash
git diff --check -- README.md docs/memory_server_api_spec.md docs/memory_server_software_implementation_design.md
```

Expected: no output.

- [ ] **Step 7: Commit Memory Server docs repair**

Run:

```bash
git add README.md docs/memory_server_api_spec.md docs/memory_server_software_implementation_design.md
git commit -m "docs: clarify memory server references"
```

Expected: commit succeeds and stages only the three files listed above.

---

### Task 5: Remove One-Off Superpowers Artifacts

**Files:**
- Delete: `docs/superpowers/plans/2026-07-06-agent-service-websocket.md`
- Delete: `docs/superpowers/plans/2026-07-07-observability-context-build-trace.md`
- Delete: `docs/superpowers/plans/2026-07-07-observability-harness-phase1.md`
- Delete: `docs/superpowers/plans/2026-07-07-observability-invariant-closure.md`
- Delete: `docs/superpowers/plans/2026-07-07-observability-memory-lifecycle-trace.md`
- Delete: `docs/superpowers/plans/2026-07-07-observability-tool-lifecycle-trace.md`
- Delete: `docs/superpowers/plans/2026-07-07-observability-trace-metrics.md`
- Delete: `docs/superpowers/plans/2026-07-07-observability-trace-viewer.md`
- Delete: `docs/superpowers/plans/2026-07-07-docs-agents-governance-reset.md`
- Delete: `docs/superpowers/specs/2026-07-06-agent-service-websocket-design.md`
- Delete: `docs/superpowers/specs/2026-07-07-docs-agents-governance-design.md`

**Interfaces:**
- Consumes: all previous task commits.
- Produces: no long-term `docs/superpowers/**` project documentation.

- [ ] **Step 1: Confirm `docs/superpowers` contents**

Run:

```bash
find docs/superpowers -type f | sort
```

Expected: output contains only files under `docs/superpowers/plans/` and `docs/superpowers/specs/`.

- [ ] **Step 2: Delete all `docs/superpowers` artifacts**

Run:

```bash
git rm -r docs/superpowers
```

Expected: git reports removal of all files under `docs/superpowers`.

- [ ] **Step 3: Verify no retained docs reference `docs/superpowers`**

Run:

```bash
rg -n "docs/superpowers" AGENTS.md README.md docs .codex/skills -g '*.md'
```

Expected: no output.

- [ ] **Step 4: Commit superpowers artifact removal**

Run:

```bash
git add docs/superpowers
git commit -m "docs: remove one-off planning artifacts"
```

Expected: commit succeeds with deletions under `docs/superpowers`.

---

### Task 6: Final Governance Verification

**Files:**
- Inspect: `AGENTS.md`
- Inspect: `README.md`
- Inspect: `docs/**`
- Inspect: `.codex/skills/**`

**Interfaces:**
- Consumes: completed cleanup commits.
- Produces: final evidence that the docs governance reset meets the approved design.

- [ ] **Step 1: Verify retained docs shape**

Run:

```bash
find docs -maxdepth 2 -type f | sort
```

Expected output contains these retained top-level files:

```text
docs/CONTEXT_ENGINEERING_STATUS.md
docs/agent-collaboration-walkthrough.md
docs/agent-communication-routing.md
docs/context-engineering-walkthrough.md
docs/gateway-architecture.md
docs/memory-module-walkthrough.md
docs/memory-service-architecture.md
docs/memory_server_api_spec.md
docs/memory_server_software_implementation_design.md
docs/observability-harness.md
docs/tool-calling-architecture.md
```

Expected output contains these retained development runbooks:

```text
docs/development/agent-pilot-operator-runbook.md
docs/development/memory-sqlite-operator-runbook.md
```

- [ ] **Step 2: Verify deleted docs references are gone**

Run:

```bash
rg -n "docs/(CURRENT_DESIGN|KNOWN_ISSUES|phase1-7|observability-local|superpowers)|docs/development/(agent-control-plane-plan|agent-production-auth-observability-plan|context-engine-memory-policy-plan|gateway-entry-layer-development-plan|memory-kernel-hardening-plan|memory-server-integration-plan|realtime-agent-interrupt-phase2-plan|realtime-agent-task-state-plan|realtime-call-agent-mvp-plan|realtime-harness-hardening-plan|realtime_phone_backend_plan)\\.md" AGENTS.md README.md docs .codex/skills -g '*.md'
```

Expected: no output.

- [ ] **Step 3: Verify README and AGENTS agree on entrypoint roles**

Run:

```bash
rg -n "coding-agent|人类|human|权威|authority|docs/development" AGENTS.md README.md
```

Expected: output shows `AGENTS.md` as the coding-agent entrypoint, `README.md` as human navigation, and `docs/development/` as non-default runbook/execution material.

- [ ] **Step 4: Run Markdown whitespace check**

Run:

```bash
git diff --check -- AGENTS.md README.md docs .codex/skills
```

Expected: no output.

- [ ] **Step 5: Run optional docs-only fast sanity check**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: tests pass. If unrelated dirty source/test changes cause failures, record the failing tests and do not modify unrelated code as part of this docs governance task.

- [ ] **Step 6: Report final worktree state**

Run:

```bash
git status --short
```

Expected: no staged docs governance changes remain. Unrelated pre-existing source/test changes may still appear and must be reported separately.

- [ ] **Step 7: Commit any final docs-only correction**

If Step 1 through Step 4 exposed a small docs-only correction, run:

```bash
git add AGENTS.md README.md docs .codex/skills
git commit -m "docs: finish governance cleanup"
```

Expected: commit succeeds only if final docs-only corrections were made. Skip this step if no final corrections were needed.
