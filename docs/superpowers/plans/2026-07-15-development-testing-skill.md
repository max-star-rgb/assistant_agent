# 开发阶段测试决策 Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 assistant_agent 日常开发提供风险驱动、不会无条件膨胀测试或运行 full 的测试决策 Skill。

**Architecture:** 新 Skill 负责当前变更的测试决策与阶段退出，测试结构继续由 `tests/README.md` 和
`tests/scope-map.toml` 管理，全仓治理继续由显式 governance Skill 负责。

**Tech Stack:** Codex Skills、Markdown、YAML、pytest。

## Global Constraints

- 所有新增文档使用中文。
- 默认测试必须 mock/local/offline。
- 不增加脚本、依赖、CI 门禁或测试数量阈值。
- 设计、实现、测试和文档验证通过后统一提交。

---

### Task 1: 建立 Skill 行为基线

**Files:**
- Modify: `tests/critical/test_scoped_test_runner.py`

- [x] 使用 fresh agent 只读重放纯重构、阶段开发和双 scope 普通功能场景。
- [x] 记录现有规则缺少统一决策、精确 full 阈值和临时测试生命周期。
- [x] 扩展仓库契约测试并确认因 Skill 文件不存在而 RED。

### Task 2: 实现测试决策 Skill

**Files:**
- Create: `.codex/skills/assistant-agent-development-testing/SKILL.md`
- Create: `.codex/skills/assistant-agent-development-testing/agents/openai.yaml`

- [x] 使用 `init_skill.py` 创建标准 Skill 目录。
- [x] 定义 ADD、EXTEND、REUSE、STAGE、NO-TEST 与阶段退出规则。
- [x] 允许隐式触发，并限制 full、integration 和全仓治理边界。

### Task 3: 接入仓库权威

**Files:**
- Modify: `AGENTS.md`
- Modify: `tests/README.md`

- [x] 增加普通行为开发自动路由。
- [x] 同步两 scope/三 scope 阈值和阶段测试归档规则。

### Task 4: 验证并交付

**Files:**
- Test: `tests/critical/test_scoped_test_runner.py`

- [x] 重放加载 Skill 后的 fresh-agent GREEN 压力场景。
- [x] 运行定向测试、critical、Skill validator 和 `git diff --check`。
- [x] 仅提交本任务文件，不 push、不合并、不创建 PR。
