# Phase 8：Assistant Brain Architecture

## 阶段目标

Phase 8 的目标是把项目从：

```text
intent-router workflow
```

升级为：

```text
assistant-driven tool loop
```

也就是：

```text
chat_node 不再只是一个可选分支
assistant_node 成为中心大脑
所有工具、模型和能力服务都变成 assistant 可以调用的 action
```

## 文档结构

```text
docs/phase8/
  README.md
  assistant-loop-architecture-upgrade.md
  planning-and-reflection-roadmap.md

task/phase8/
  README.md
  assistant-loop-mvp.md
  planning-followup.md
  reflection-followup.md

prompt/phase8/
  run-assistant-loop-mvp.md
  run-planning-followup.md
  run-reflection-followup.md
```

## 新规范

Phase 8 开始采用以下规范：

```text
task 文件负责完整任务说明
prompt 文件只负责启动执行
```

也就是说：

```text
Read first / Scope / Requirements / Acceptance / Stop condition
```

必须写在：

```text
task/phase8/*.md
```

而不是写在 prompt 里。

prompt 只应该告诉 Codex / Claude Code：

```text
执行哪个 task
遵守 task 里的 Read first / Scope / Requirements / Acceptance
完成后停止
```

## 推荐执行顺序

```text
Phase 8A Assistant Loop MVP
  ↓
Phase 8B Planning Follow-up
  ↓
Phase 8C Reflection Follow-up
```

先只执行 Phase 8A。不要在第一轮同时实现 planning 和 reflection。
