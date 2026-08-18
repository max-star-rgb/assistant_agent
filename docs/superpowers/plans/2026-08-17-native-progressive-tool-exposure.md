# 原生渐进式 Tool 暴露实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 fast `create_agent` 主链中恢复“加载 Skill 后才向模型暴露其管辖 Tool”的确定性行为，同时继续使用标准 `BaseTool`、`ToolNode`、middleware 和 Graph state。

**Architecture:** 进程启动时仍装配完整静态 Tool inventory；新增一个 `AgentMiddleware`，每次模型调用前根据 checkpoint 中的 `active_skill_ids` 过滤 `ModelRequest.tools`。`load_skill` 成功的标准 `ToolMessage` 由同一 middleware 转换为包含该消息和窄状态更新的 `Command`；Tool 名单始终从受信 `skill.toml` 的 `governed_tools` 重新解析，不接受模型提供任意 Tool 名。

**Tech Stack:** Python 3.12、LangChain `create_agent` middleware、LangGraph `Command`/state reducer、Pydantic、pytest。

## Global Constraints

- 默认测试与验证使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 Provider。
- 不恢复旧 `ToolRegistry`、`ContextService`、`ActionValidator` 或第二套 Agent loop。
- Tool inventory 与 ToolNode 仍注册全部标准 `BaseTool`；动态逻辑只改变单次模型可见 schema。
- Tool exposure 不替代具体 Tool 的身份、参数、授权和副作用校验。
- 临时 RED/GREEN 测试只放入 `tests/tdd/progressive-native-tool-exposure/`。

---

### Task 1: 定义渐进暴露状态与 middleware

**Files:**
- Create: `src/assistant_agent/native_agent/tool_exposure.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Test: `tests/tdd/progressive-native-tool-exposure/test_progressive_tool_exposure.py`

**Interfaces:**
- Consumes: `SkillCatalog`、`SkillDescriptor`、`ModelRequest.tools`、成功的 `load_skill` `ToolMessage.artifact`。
- Produces: `ProgressiveToolExposureMiddleware`、`active_skill_ids`、`skill_reference_grants`。

- [x] 编写失败测试：未加载 Skill 时隐藏 `governed_tools`，非管辖 Tool 保持可见。
- [x] 显式运行 feature 测试并确认因 middleware 尚不存在而失败。
- [x] 实现最小 model-call filter，并用去重 reducer 保存 Skill 激活状态。
- [x] 编写失败测试：成功 `load_skill` 结果产生包含原 ToolMessage 的 `Command`，失败/伪造结果不授权。
- [x] 实现 sync/async tool-call wrapper，并运行测试至通过。

### Task 2: 接入 fast agent 与 reference grant

**Files:**
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Modify: `src/assistant_agent/tools/base.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/skill_loading/tool.py`
- Test: `tests/tdd/progressive-native-tool-exposure/test_progressive_tool_exposure.py`

**Interfaces:**
- Consumes: Task 1 的 middleware 和 state channels。
- Produces: `build_fast_agent` 中的原生渐进暴露；`LoadSkillReferenceTool` 可读取 checkpoint 中的窄 reference grant。

- [x] 编写失败测试：同一 fast agent 首次模型调用仅见核心 Tool，执行 `load_skill` 后下一次调用可见对应业务 Tool。
- [x] 将 middleware 装入 `create_agent`，并把可发现 Skill 的 L0 index 注入 dynamic system prompt。
- [x] 让 `ToolContext.skill_reference_grants` 从受信 Graph state 读取。
- [x] 运行 feature 测试确认 RED→GREEN。

### Task 3: 对齐 manifest、文档与验证

**Files:**
- Modify: `skills/travel-tool-orchestration/skill.toml`
- Modify: `skills/workspace-communications/skill.toml`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/context_engineering_status.md`

**Interfaces:**
- Consumes: native inventory 的最终模型可见 Tool 名。
- Produces: 与 `<namespace>_<server>_<tool>` 一致的 `governed_tools`，以及当前 authority 描述。

- [x] 对齐已经迁移为官方 MCP adapter 的 Tool 名，不保留不会匹配 inventory 的旧 dotted name。
- [x] 更新 Tool/Context authority，明确静态注册与动态模型可见集合的区别。
- [x] 运行临时 feature pytest、相关最小 core contract 和文档 authority validator。
- [x] 复核 diff，只报告本任务修改，不覆盖工作区其他改动。
