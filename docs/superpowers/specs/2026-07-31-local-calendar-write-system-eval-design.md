# 本地日历写入 Tool System Eval 设计

## 1. 目标

在 `evals/system/tools` 下新增一个可显式运行的日历写入 system eval，验证
`calendar_create` 对真实本地 SQLite 日历的执行结果。

本 eval：

- 不调用 LLM；
- 不创建或运行 `AgentGraphRuntime`；
- 不经过 assistant loop；
- 保留 `ActionValidator -> ToolExecutor -> ToolRegistry -> CalendarCreateTool`
  治理链；
- 不访问网络或真实 Provider；
- 不写入开发者日常使用的默认日历数据库。

这里的“与 runtime 无关”是指不验证主 Runtime 的模型决策、循环和终态行为。为满足仓库的工具调用
硬边界，runner 仍复用 `assistant_agent.runtime` 中的 `ActionValidator`、`ToolExecutor`、
`UserRequest` 和 `AgentState` 作为工具治理与执行载体。

## 2. 目录与入口

新增：

```text
evals/system/tools/calendar_write.py
scripts/run_system_calendar_write_eval.py
```

并更新：

```text
evals/README.md
scripts/README.md
```

不恢复、覆盖或依赖当前工作区中已删除的：

```text
evals/system/tools/runner.py
evals/system/tools/cases.json
```

稳定命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_write_eval.py \
  --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_write_eval.py \
  --allow-local-calendar-write
```

`--dry-run` 只校验配置并展示即将写入的事件与目标路径。真正执行必须显式提供
`--allow-local-calendar-write`。

## 3. 数据隔离

每次运行先通过 system eval 的 artifact helper 创建独立目录：

```text
.data/evals/system/tools/calendar/<run_id>/
```

该目录包含：

```text
calendar.sqlite3
summary.json
result.json
```

`LocalSQLiteCalendarAdapter` 指向该次运行自己的 `calendar.sqlite3`。这是真实 SQLite 建表、事务、
唯一索引、查询与落盘，不使用 mock；同时不会污染默认的
`.data/calendar/events.sqlite3`。

事件使用独立 namespace，例如 `system-calendar-write-eval`。数据库作为证据随 artifact 保留，
operator 可通过删除整个 run 目录完成回收，不需要为生产日历引入 delete/reset 接口。

## 4. 执行流程

runner 按以下固定顺序执行：

1. 创建 run artifact 目录和 `LocalSQLiteCalendarAdapter`。
2. 以该 adapter 构造 `CalendarCreateTool`。
3. 将唯一的 `calendar_create` 注册到新的 `ToolRegistry` 并 seal。
4. 创建 foreground `UserRequest` 和对应 `AgentState`，不构造
   `AgentGraphRuntime`。
5. 创建 `AssistantToolCall`。模型可见输入只包含事件业务字段，不包含
   `idempotency_key`。
6. 使用 `ActionValidator.validate()` 校验工具注册、输入 schema 和 runtime-owned 字段边界。
7. 校验通过后，调用 `ToolExecutor.run_tool()`；通过其受信
   `runtime_input={"idempotency_key": <run-scoped-key>}` 注入幂等键。
8. 对同一业务输入和同一幂等键再执行一次完整的 validator/executor 流程。
9. 使用 adapter 的 `snapshot()` 和 `diff()` 读取真实 SQLite 终态并生成结构化检查结果。
10. 写入 artifact，并根据检查结果返回进程退出码：通过为 `0`，验证失败为 `1`，配置或授权失败为
    `2`。

第二次调用仍重新经过 validator 和 executor，不直接调用 adapter，也不复用第一次
`ToolResult`。

## 5. 输入

默认提供稳定的合成事件，避免写入真实个人信息：

```text
title: assistant_agent 本地日历写入评测 <run_id>
start_time: 2030-01-15T09:00:00+08:00
end_time: 2030-01-15T09:30:00+08:00
timezone: Asia/Shanghai
location: system-eval
attendees: []
notes: synthetic system eval event
```

CLI 可以覆盖标题和时间，但仍只允许写入本次 run 的隔离数据库。幂等键由 runner 生成并通过
`runtime_input` 注入，不能由普通 Tool call 参数提供。

## 6. 通过条件

结果至少包含以下结构化检查：

- Registry 只注册 `calendar_create`；
- ToolSpec 的 `category` 为 `write`；
- 两次 ActionValidator 均返回 `accepted`；
- 每次执行恰好新增一个 governed tool call record，最终 state 中按顺序共有两个
  `calendar_create` call record；
- 第一次 `ToolResult.success=true`；
- 第一次结果的 `provider=local_sqlite`；
- 第一次结果的 `side_effect_level=committed`；
- 第二次 `ToolResult.success=true`；
- 第二次结果的 `side_effect_level=idempotent_replay`；
- 两次结果返回相同的非空 `event_id`；
- SQLite 最终只有一条属于本 namespace 的事件；
- 事件标题、开始时间、结束时间、时区、地点、参会人、备注和幂等键与输入完全一致；
- state diff 只有一条 added event，没有 modified、deleted 或 duplicate group。

`result.json` 保存上述 checks、failures、两次脱敏后的结构化 ToolResult、snapshot/diff 和 run
标识。`summary.json` 只保存总体通过状态、失败项、artifact 路径和事件 ID，不保存个人数据。

## 7. 安全与失败处理

- 不要求 `MULTIMODAL_AGENT_PROVIDER_MODE=real`，因为 SQLite 本地工具不是 Provider。
- 不读取 `.env`，不检测或使用 API key。
- 不调用默认 registry，避免意外装配其他工具或 Provider。
- 未提供 `--allow-local-calendar-write` 时，在创建数据库和执行 Tool 前失败。
- artifact 根路径必须是明确的 run 子目录，不能接受 workspace 根目录、用户主目录或默认日历路径。
- ActionValidator 拒绝、Tool 执行失败、幂等回放失败、SQLite 终态不匹配都生成可解释
  `failures`，而不是静默通过。
- 不自动删除成功或失败 artifact，以便复核真实写入证据。

## 8. 测试与验证边界

这是具体 builtin Tool 的正式本地 system eval，不改变任何已登记 core invariant，不新增或修改
`tests/core`，也不新增 pytest。

实现后的最小验证为：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_write_eval.py \
  --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_write_eval.py \
  --allow-local-calendar-write
```

完成汇报格式：

```text
Core invariant: unchanged.
Tests: not added because this is a concrete builtin Tool system eval.
System eval: ran the isolated real local SQLite calendar write and idempotent replay checks.
```

本 eval 不证明 LLM 会正确选择 `calendar_create`，也不证明完整 Runtime 能完成日历任务；这些属于
`evals/agent` 或单独的 runtime 验证范围。
