# 上游原生 Skills 体系迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Deep Agents `SkillsMiddleware + FilesystemMiddleware(read_file)` 替换项目自研 Skill loader、reference grant 和重复目录快照。

**Architecture:** fast 与 planning 各装配共享 Skill backend 上的上游 Skills/Filesystem middleware；`SkillsMiddleware.before_agent` 成为唯一目录来源，`read_file` 成为唯一正文与 supporting file 读取入口。业务 Tool inventory 不再注册 Skill loader，Planner 仅通过 task description 传递必要规则。

**Tech Stack:** Python 3.12、LangChain `create_agent`、Deep Agents 0.7.8、LangGraph、pytest。

**Spec:** `docs/superpowers/specs/2026-08-26-upstream-native-skills-design.md`

## Global Constraints

- `read_file` backend 只允许仓库 `skills/` 虚拟根。
- `FilesystemMiddleware` 只注册 `read_file`，不注册其他文件或执行 Tool。
- fast/planning 最多 12 次 model call；`read_file` 纳入同参数一次、不同参数十二次的 per-Tool limit。
- Skill 与 Tool Profile 保持独立。
- 默认 mock/offline，不调用真实 Provider。
- 工作区已有重叠改动，本计划不执行 Git commit。

---

### Task 1: 用测试定义上游原生 Skill 契约

**Files:**
- Create: `tests/tdd/upstream-native-skills/test_upstream_native_skills.py`
- Modify: `tests/core/contract/test_extension_contract.py`
- Modify: `tests/core/integration/test_context_lifecycle.py`

**Interfaces:**
- Consumes: `build_fast_agent(...)`、`build_planning_agent(...)`、`create_native_tool_inventory(...)`。
- Produces: fast/planning 均暴露上游 `read_file`，生产 inventory 不含自研 loader，task state 不含自研 Skill channel 的回归契约。

- [x] 写临时测试：创建虚拟 Skill，断言 model 首轮看到 `SkillsMiddleware` 生成的目录和 `read_file`，调用 `read_file("/skill-sentinel/SKILL.md")` 后获得正文。
- [x] 更新 EXT-001：断言 inventory 不含 `file_read`、`load_skill`、`load_skill_reference`；受限 `read_file` 由 middleware 提供而不属于业务 inventory。
- [x] 更新 CTX-001：删除 Planner Skill grant 继承测试，改为断言 worker 输入不包含父 conversation、Todo 和任何自研 Skill state。
- [x] 运行定向测试并确认因当前仍使用自研 loader 而 RED：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/upstream-native-skills \
  tests/core/contract/test_extension_contract.py \
  tests/core/integration/test_context_lifecycle.py
```

### Task 2: 迁移 fast/planning composition

**Files:**
- Modify: `src/assistant_agent/skills/native.py`
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Modify: `src/assistant_agent/native_agent/planning_agent.py`
- Modify: `src/assistant_agent/native_agent/state.py`

**Interfaces:**
- Produces: `create_project_skills_middleware(backend) -> SkillsMiddleware` 和 `create_project_skill_filesystem_middleware(backend) -> FilesystemMiddleware`。
- Consumes: 同一 `BackendProtocol` 实例、上游 middleware `tools` 属性。

- [x] 将 `create_project_skills_middleware` 恢复为使用上游默认 system prompt，新增只注册 `read_file` 的 Filesystem middleware factory，删除手动 metadata snapshot/正文/reference helper。
- [x] fast 装配两个上游 middleware；从自定义 dynamic prompt 删除 Skill 目录和 loaded-state 逻辑；把 middleware 注入的 `read_file` 纳入 per-Tool limiter。
- [x] planning 删除 Skill Tool、自定义目录 prompt和 selective Skill bridge；装配同一两个上游 middleware，并让 compiled worker wrapper 只负责输入隔离与结果投影。
- [x] 从 Fast/Planning state 删除自研 Skill channel。
- [x] 运行 Task 1 测试直到 GREEN。

### Task 3: 删除自研 Skill loader 生产链

**Files:**
- Modify: `src/assistant_agent/native_agent/tools.py`
- Modify: `src/assistant_agent/tools/ids.py`
- Modify: `src/assistant_agent/tools/runtime.py`
- Delete: `src/assistant_agent/tools/plugins/builtin/skill_loading/__init__.py`
- Delete: `src/assistant_agent/tools/plugins/builtin/skill_loading/models.py`
- Delete: `src/assistant_agent/tools/plugins/builtin/skill_loading/plugin.py`
- Delete: `src/assistant_agent/tools/plugins/builtin/skill_loading/tool.py`
- Modify: `src/assistant_agent/improvement/evaluator.py`

**Interfaces:**
- Produces: 业务 inventory 不含文件/Skill loader；Skill 文件读取完全由 Agent middleware 注册。

- [x] 从 `_builtin_plugins` 删除 `SkillLoadingPlugin`，并移除只为该 plugin 传递的 backend 参数。
- [x] 删除 loader Tool ID、runtime grant helper、loader 包和无引用 model。
- [x] evaluator 改为直接读取 `SkillsMiddleware.before_agent` 产生的 `skills_metadata`，不依赖已删除 snapshot helper。
- [x] 用 `rg` 确认生产源码不存在 `load_skill`、`load_skill_reference`、`loaded_skill_ids` 或 grant state。
- [x] 运行 Task 1 测试及 `tests/core/integration/test_runtime_lifecycle.py`。

### Task 4: 同步 Skill 文档与权威并验收

**Files:**
- Modify: `skills/*/SKILL.md`（只修改仍引用自研 loader 的文件）
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/context_engineering_status.md`
- Modify: `tests/core/INVARIANTS.md`

**Interfaces:**
- Produces: 文档只描述 `before_agent → system prompt → read_file` 原生链。

- [x] 删除文档中的自研 loader/grant/继承描述，明确 `read_file` 只绑定 Skill 虚拟根。
- [x] 更新 EXT-001 与 CTX-001 的结构化契约。
- [x] 运行相关 core/TDD、Skill validator、文档 authority validator、`compileall` 和 `git diff --check`。
- [x] 重启或等待唯一 8089 热重载，确认新进程加载时间晚于源码修改且 `/ok` 返回 200。
