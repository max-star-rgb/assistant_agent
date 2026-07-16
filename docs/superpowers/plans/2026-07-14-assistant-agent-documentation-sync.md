# assistant_agent 文档同步与清理实施计划

> 状态：development record。本文只记录 2026-07-14 的实施范围与验证约定，不是当前架构权威；当前事实仍以源码、测试、`AGENTS.md` 路由及各专项 authority 为准。

## Summary

新增仅由用户显式请求触发的项目 skill，用确定性脚本收集文档漂移证据，再完成本轮 `README.md`、`AGENTS.md`、权威文档、专项 skills 和失效文档清理。不会设置 `allow_implicit_invocation`，不会修改运行时公共 API，也不会触碰当前工作树中的 streaming-video 等无关文件。

## Implementation Changes

1. 用 TDD 建立只读文档证据采集器：CLI 接受 `--repo-root` 和可选 `--git-range`，只向 stdout 输出含 `schema_version=1`、文档清单/分类、Git 变更、last-touch、入站引用及 Markdown/仓库路径检查的 JSON。无 range 时不猜基线；本轮增量范围为 `9693c65..HEAD`，同时审计完整 `docs/**`。无效仓库或 revision range 非零退出，失效链接只报告不修改。
2. 创建显式调用的 `.codex/skills/assistant-agent-documentation-sync/`，包含 `SKILL.md`、仅有 `interface` 的 `agents/openai.yaml` 和脚本。固定流程是读 AGENTS、保护 dirty worktree、收集全量/增量证据、建立能力/权威映射、同步入口/专项 skill、清理证据充分的失效文档并验证。删除必须已有替代权威、无独有操作价值且引用可修复；否则只列候选。用 fresh-agent 场景验证显式触发边界。
3. 将 `docs/runtime-event-stream-architecture.md` 升级为 runtime streaming 当前权威，覆盖 `LLMEvent`、provider stream、`AgentEvent`、`AgentRunStream`、stream/result 分离、线程边界、取消限制、源码映射和验证；新增显式专项 skill，并删除被吸收的 provider event/thread audit 文档。
4. 同步文档体系：README 保持轻导航；AGENTS 补齐 documentation-sync、runtime streaming 和新增服务职责；memory authority/skill/walkthrough 覆盖 typed facts、冲突治理、FTS、framework adapter/bake-off；context walkthrough/skill 覆盖 editable owner、realtime task/video 和 durable task context；roadmap 继续作为 north star 并移除已完成能力的缺失陈述。保留 agent pilot、realtime runtime、memory dual-core/SQLite/framework bake-off runbook。
5. 删除被 tool-calling 权威替代的旧 tool-system evolution 说明，以及被 Media-Agent WebSocket contract 替代的旧媒体侧 H.264 指南。保留其他 `docs/superpowers/specs/**` 与 `plans/**`，明确其为开发记录而非当前权威。
6. 批准的设计、本计划、代码、测试、skill 与文档在全部验证后统一提交，不单独提交设计材料。

## Public Interfaces

新增只读 CLI：

```bash
python .codex/skills/assistant-agent-documentation-sync/scripts/collect_documentation_evidence.py \
  --repo-root . \
  --git-range 9693c65..HEAD
```

新增显式项目 skills：

- `$assistant-agent-documentation-sync`

不改变 FastAPI、Gateway、tool、memory、provider 或 runtime 的 Python/API 契约。

## Test Plan

- 证据脚本单元及临时 Git 仓库集成测试覆盖位置分类、稳定排序/schema、add/modify/delete/rename、Markdown 相对链接/锚点/外部 URL/仓库路径/glob、入站引用、last-touch、合法/非法 range、非 Git 仓库与运行前后 tracked files 不变。
- 对新增 skills 运行 `quick_validate.py` 并确认 metadata 不含 `policy`。
- 删除后运行 evidence collector、悬空引用搜索和 `git diff --check`。
- 运行 runtime streaming、memory facts/conflict/ranking/framework/config、context/realtime video/durable context，以及 roadmap 涉及的 turn arbitration、proactive wake、Improvement Lab 最小离线测试。
- 最后运行 `scripts/check_env.py` 和 `pytest -m fast -q`；不调用真实外部 Provider。

## Assumptions And Delivery

- 当前源码、测试、配置和实际命令优先，Git 历史只定位变化。
- 不批量删除 superpowers specs/plans；证据不足的旧文档只列候选。
- 保留用户及并行工作的未跟踪/已修改文件，提交时只暂存本任务文件。
- 验证通过后创建本地提交 `feat(docs): add governed documentation sync workflow`；不 push、不合并、不创建 PR。
