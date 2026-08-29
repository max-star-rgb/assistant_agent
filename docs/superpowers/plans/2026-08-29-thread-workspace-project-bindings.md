# Product Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让持久 `workspace_id` 管理上下文/artifacts，让 thread 只拥有 scratch/uploads，同时允许本机 Agent 按 Linux 用户权限直接操作任意真实路径。

**Architecture:** 复用当前 Workspace metadata 和受信 runtime metadata；Deep Agents 原生 `LocalShellBackend(virtual_mode=False)` 以 Agent Home 为 cwd，绝对路径直接落到宿主文件系统。Workspace 只持久化 artifacts、thread runtime 和未来的逻辑 project references。

**Tech Stack:** Python 3.12、Pydantic、Deep Agents native backends、LangGraph ToolRuntime、pytest。

**Spec:** `docs/superpowers/specs/2026-08-29-thread-workspace-project-bindings-design.md`

## Global Constraints

- 默认 mock/offline，不调用真实 Provider。
- Workspace 总根为 `Path.home() / "assistant_agent" / "workspaces"`。
- `workspace_id` 只能来自受信 runtime facts；文件路径由 filesystem/shell 正常接收并受统一 HITL 与 OS 权限约束。
- 产品源码不自动注册为 project；本次不新增 project CRUD、Git worktree 或 apply Tool。
- 临时 RED/GREEN 测试只放 `tests/tdd/thread-workspace-project-bindings/`。

---

### Task 1: 持久 Workspace 与 thread 目录

**Files:**
- Modify: `src/assistant_agent/worktree/manager.py`
- Test: `tests/tdd/thread-workspace-project-bindings/test_workspace_manager.py`

**Interfaces:**
- Produces: `resolve_workspace(identity, workspace_id, thread_id)`。
- Produces: Workspace 级 `workspace.json/artifacts` 和 thread 级 `scratch/uploads`。

- [ ] 写测试：两个 thread 共享同一 Workspace artifacts，但 scratch 不同，且没有物理 projects 目录。
- [ ] 写测试：同一 workspace ID 拒绝另一个 identity。
- [ ] 运行测试确认旧签名/路径导致 RED。
- [ ] 最小修改 metadata、路径和 TTL 清理后运行测试确认 GREEN。

### Task 2: 受信上下文与资源消费方

**Files:**
- Modify: `src/assistant_agent/native_agent/context.py`
- Modify: `src/assistant_agent/worktree/backend.py`
- Modify: `src/assistant_agent/mcp/stateful_sessions.py`
- Modify: Artifact 生成调用方
- Test: `tests/tdd/thread-workspace-project-bindings/test_workspace_integration.py`

**Interfaces:**
- Produces: `AssistantRuntimeFacts.workspace_id`。
- Consumes: backend/MCP/Artifact 从 runtime facts 读取 Workspace，不从 thread 推导。

- [ ] 写测试并确认缺少 workspace ID 传播时 RED。
- [ ] 实现 `virtual_mode=False` host filesystem、scratch/uploads/artifacts 快捷路由和 Artifact 路径。
- [ ] 运行 Workspace/MCP/Artifact 定向测试确认 GREEN。

### Task 3: Composition 与只读 worker

**Files:**
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/agent_server/async_delegation.py`
- Modify: worker backend/state 的直接调用方
- Test: 现有 async/worker 临时测试与受影响 core invariant 测试

**Interfaces:**
- Removes: 产品源码静态 repository、默认 `/projects/assistant-agent`、Git worktree/apply Tool composition。
- Produces: child worker 继承父 `workspace_id` 并只读 Workspace。

- [ ] 写/改失败测试：composition repository 清单为空，worker metadata 保留父 workspace ID。
- [ ] 删除静态源码 project/apply 装配，迁移 worker backend 与 async metadata。
- [ ] 运行 worker/async/core 定向测试确认 GREEN。

### Task 4: Authority 与验证

**Files:**
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`

- [ ] 回补当前 authority。
- [ ] 运行临时 TDD、受影响 core、默认 core、ruff、compileall、authority validator 和 `git diff --check`。
- [ ] 验证现有 8089 hot reload 健康；不启动第二套 Server，不调用真实 Provider。
