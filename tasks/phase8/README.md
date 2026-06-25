# Phase 8 Task Index

## 规范

Phase 8 开始：

```text
task 文件负责完整任务说明
prompt 文件只负责启动执行
```

每个 task 必须包含：

```text
Read first
Scope
Requirements
Tests
Acceptance
Stop condition
```

## 推荐执行顺序

```text
assistant-loop-mvp.md
planning-followup.md  # ReAct plan mode, not parallel plan_and_solve strategy
reflection-followup.md
```

先只执行：

```text
tasks/phase8/assistant-loop-mvp.md
```

不要一开始执行 planning/reflection。
