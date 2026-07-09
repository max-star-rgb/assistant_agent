# Realtime Assistant Loop Design

## Goal

把 Phase 1 从“已有文本实时骨架”做深到第一版可开发的 Personal Realtime Assistant Loop：

```text
Phone / WebSocket text input
  -> Gateway Session
  -> Turn Manager
  -> GatewayAgentAdapter
  -> AgentGraphRuntime
  -> ToolExecutor
  -> Stream Response
  -> Interrupt / Cancel / Hangup
  -> Trace
```

这个设计只覆盖 Phase 1。Phase 2 Memory Intelligence 和 Phase 3 Skill v1 只作为后续依赖，不在本文展开。

## Current State

当前项目已经具备 Phase 1 的基础骨架：

- `/ws/realtime/media` 接受媒体服务事件，并映射到 Gateway frames。
- `GatewaySessionService` 管理 `message.user`、`run.cancel`、active run、pending message、interrupt、deadline 和 session history。
- `GatewayAgentAdapter` / `AgentGraphRealtimeBackend` 把 `RealtimeAgentRequest` 转成 `UserRequest`，再进入 `run_assistant_request` 和 `AgentGraphRuntime`。
- 工具执行仍走 `ActionValidator -> ToolExecutor -> ToolRegistry`。
- `scripts/run_realtime_call_simulator.py` 已能跑 text-only `basic`、`interrupt`、`hangup` 三类场景。
- `run.end.payload.trace_id` 已能把 Gateway 结果和 trace 查询连接起来。

当前缺口不是再造一个 realtime runtime，而是把一次真实实时会话中的 turn 状态、取消输出门禁、工具运行中打断、stream 顺序和 trace 验收固化下来。

## Non-Goals

本阶段不做：

- ASR、TTS、VAD、音频流、语音克隆或声学模型。
- 新 Agent loop。
- Multi-agent routing、agent swarm、远程 agent 自动发现。
- Memory Intelligence 详细设计。
- Skill marketplace、用户上传 skill、workflow engine。
- RL、自动学习、自动改 prompt 或自动改生产策略。
- 真实 provider 默认调用。

媒体服务负责语音输入输出；本仓库只处理 finalized text turn、Gateway 生命周期、Agent runtime 调用和文本输出。

## Design Options

### Option A: Continue Inside `GatewaySessionService`

把所有 turn 规则继续写在 `GatewaySessionService` 内。

优点是改动少，贴近现状。缺点是 active run、pending queue、interrupt、cancel、hangup、deadline、trace 继续集中在一个文件里，后续测试会越来越难读。

### Option B: Add A Small Turn Manager Boundary

新增一个很薄的 Turn Manager 责任边界，专门回答一件事：当前 session 收到一个事件后，应该 start、queue、cancel、interrupt、drop 还是 end。

优点是状态规则更清楚，测试可以直接覆盖 turn 决策。缺点是需要从 `GatewaySessionService` 拆出一小块状态逻辑。

### Option C: Build A Separate Realtime Core

新建一个更完整的 realtime core，统一 session、turn、stream、cancel 和 trace。

优点是看起来完整。缺点是过早架构化，容易变成第二套 Gateway，也会把当前稳定的 Gateway 边界打散。

### Recommended Approach

采用 Option B，但保持克制：

- Turn Manager 是 Gateway 内部的小边界，不是新 runtime。
- 第一版只管理文本 turn 状态，不理解用户语义。
- `GatewaySessionService` 仍负责 endpoint IO、frame 映射和调用 backend。
- `GatewayAgentAdapter` 仍是唯一进入 `AgentGraphRuntime` 的 realtime adapter。

如果实现时发现当前 `GatewaySessionService` 里的状态逻辑还能通过小函数稳定表达，可以先把 Turn Manager 作为逻辑责任落在测试和私有 helper 中；只有当测试明显难以表达时，再抽到 `src/assistant_agent/gateway/turn_manager.py`。

## Responsibilities

### Entry Adapter

负责接入外部协议：

- `/ws/realtime/media`
- `/ws/gateway`
- 本地 simulator
- 未来电话 SDK 或 WebSocket bridge

它只做：

- 验证输入。
- 绑定 user/session。
- 把媒体服务事件转成 Gateway frames。
- 把 Gateway 文本输出交给外部 TTS 或 UI。

它不做工具选择、记忆策略、Agent 决策或 provider 调用。

### Gateway Session

负责实时会话生命周期：

- session 复用。
- active run 注册。
- pending message 队列。
- `run.started` / `stream.chunk` / `event.progress` / `run.end` 输出。
- `run.cancel`、interrupt、hangup、disconnect、deadline。
- session history snapshot。

Gateway Session 不负责理解用户意图，也不直接调用工具。

### Turn Manager

负责 per-session turn 状态规则：

```text
idle + user_text               -> start
running + user_text            -> queue
running + interrupt_user_text  -> cancel_active_then_start
running + run.cancel           -> cancel_active
running + session.end          -> cancel_active_and_hangup
idle + session.end             -> hangup_ack_only
```

Turn Manager 的输入是 Gateway 已验证过的控制事件；输出是 Gateway Session 能执行的动作。

它不接触 `AgentGraphRuntime`、ToolExecutor、Memory 或 provider。

### GatewayAgentAdapter

负责把 Gateway turn 变成 assistant runtime request：

```text
RealtimeAgentRequest
  -> UserRequest
  -> run_assistant_request
  -> AgentGraphRuntime
```

它可以转发进度、工具事件、最终文本和 trace id。

它不拥有主大脑，不选择工具，不写记忆，不做 multi-agent routing。

### Agent Runtime And Tool System

`AgentGraphRuntime` 仍是唯一主执行器。工具执行仍走：

```text
AssistantDecision
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
```

Realtime cancel 是外层生命周期信号。工具如果不能立刻停止，Gateway 也必须阻止旧 run 的后续输出继续发给用户。

## Turn Rules

### Basic Turn

```text
session.start
  -> call.ready
transcript.final
  -> run.started
  -> event.progress / stream.chunk
  -> run.end(reason=completed, trace_id=...)
session.end
  -> call.hangup_ack(cancelled_active_run=false)
```

验收重点：

- 一个输入只产生一个 terminal `run.end`。
- `run.end` 包含 `trace_id`。
- 已完成 run 后挂断不会误报取消。

### Queued Turn

```text
running + normal transcript.final
  -> queued
active run completed
  -> next run.started
  -> next run.end
```

验收重点：

- 普通新输入不打断当前 run。
- 第二个 turn 使用同一个 session。
- session history 按顺序传入 backend。

### Interrupt Turn

```text
running + transcript.final(interrupt=true)
  -> cancel active run
  -> old run.end(reason=cancelled)
  -> new run.started
  -> new run.end(reason=completed)
```

验收重点：

- 旧 run 的后续 chunk 被丢弃。
- 新 turn 的 metadata 标记 interrupt。
- 旧 run 和新 run 的 `run_id` 不同。
- session 不断开。

### Explicit Cancel

```text
running + run.cancel
  -> active cancel token set
  -> run.end(reason=cancelled)
```

验收重点：

- 只取消匹配的 session/run。
- 未找到 run 时返回 `run_not_found`。
- cancel metadata 包含来源和原因。

### Hangup

```text
running + session.end
  -> cancel active run
  -> call.hangup_ack(cancelled_active_run=true)
  -> run.end(reason=cancelled)
```

如果 session 已 idle：

```text
idle + session.end
  -> call.hangup_ack(cancelled_active_run=false)
```

验收重点：

- 挂断会清理 pending turn。
- 挂断不会启动新 run。
- hangup ack 不依赖 Agent runtime 成功返回。

### Tool-Running Interrupt

```text
tool running
  -> user interrupt
  -> cancel active run
  -> suppress stale tool output
  -> old run.end(reason=cancelled)
  -> new run proceeds
```

第一版只要求 best-effort cancel：

- 如果工具支持 cancel，应尽快停止。
- 如果工具无法立即停止，Gateway 必须停止向用户发送旧 run 输出。
- 后台任务完成后不能再覆盖新 run。
- trace 需要记录 cancel source 和 best-effort 状态。

## Stream Response Rules

输出顺序必须稳定：

```text
run.started
event.progress*
stream.chunk*
run.end
```

取消后：

- 不再发送旧 run 的 `stream.chunk`。
- 不再发送旧 run 的 `response.final` 内容。
- 可以发送旧 run 的 `run.end(reason=cancelled)`。
- 新 run 的 frames 必须带自己的 `run_id` 和 `turn_id`。

`event.progress` 是展示状态，不是最终回答。TTS 入口可以选择读 `stream.chunk`，也可以选择部分 display-only progress，但它不改变 runtime。

## Trace Requirements

每次 realtime turn 至少要能回答：

- 什么 session 收到了什么类型的 turn。
- 什么时候进入 run。
- 是否排队、取消、打断或挂断。
- 是否进入 `AgentGraphRuntime`。
- 是否调用工具。
- 是否被 cancel token 终止。
- 最终 `run.end` 的 reason 是什么。
- 对应 `trace_id` 是什么。

建议新增或强化的 prompt-safe trace 点：

```text
gateway.turn.started
gateway.turn.queued
gateway.turn.interrupted
gateway.run.cancel_requested
gateway.run.cancelled
gateway.turn.completed
realtime.backend.finished
```

这些 trace 不能包含用户原文、完整 prompt、原始 memory、provider raw response、密钥或媒体 body。

## Acceptance Scenarios

第一版 Realtime Assistant Loop 做深后，必须通过这些场景：

| scenario | expected result |
| --- | --- |
| basic | start -> transcript -> stream -> completed -> hangup ack |
| queued | running 时普通输入排队，前一轮完成后再执行 |
| interrupt | running 时 interrupt 取消旧 run 并启动新 run |
| explicit cancel | `run.cancel` 取消 active run |
| hangup while running | `session.end` 取消 active run 并 ack |
| tool-running interrupt | 工具执行中打断，旧输出被压住，新 turn 正常开始 |
| trace lookup | 每个 terminal run 能通过 `trace_id` 查到安全摘要 |

## Implementation Slices

后续实施计划应按以下切片推进。

### Slice 1: Turn State Contract

新增或强化测试：

- idle 收到 transcript 会 start。
- running 收到普通 transcript 会 queue。
- running 收到 interrupt 会 cancel active。
- running 收到 hangup 会 cancel active 并清 pending。

可能修改：

- `src/assistant_agent/gateway/session.py`
- 可选新增 `src/assistant_agent/gateway/turn_manager.py`
- `tests/test_gateway_session.py`

### Slice 2: Explicit Cancel And Output Gate

新增测试：

- `run.cancel` 后不再发送旧 `stream.chunk`。
- backend 晚返回时不能覆盖新 run。
- cancel metadata 出现在 result/trace 中。

可能修改：

- `src/assistant_agent/gateway/session.py`
- `tests/test_gateway_api.py`

### Slice 3: Tool-Running Interrupt

新增一个 deterministic fake tool/runtime 场景，模拟工具执行中等待 cancel。

验收：

- interrupt 后旧 tool 输出不进入用户 stream。
- 新 run 可以继续完成。
- trace 或 run metadata 标记 best-effort cancel。

可能修改：

- `tests/test_realtime_agent_backend.py`
- `tests/test_gateway_api.py`
- `src/assistant_agent/realtime/agent_graph_backend.py`

### Slice 4: Simulator Deepening

扩展 `scripts/run_realtime_call_simulator.py`：

- 保留 `basic`、`interrupt`、`hangup`。
- 新增 `cancel`。
- 新增 `tool_interrupt` 或等价 deterministic slow-tool 场景。

验收命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_realtime_call_simulator.py --scenario all --quiet
```

### Slice 5: Trace Gate

新增 Phase 1 deep gate 测试，检查：

- terminal `run.end` 都有 reason。
- completed/error/cancelled 都能找到 trace id 或明确的 no-trace reason。
- cancel/interrupt/hangup 能看到 prompt-safe cancel source。
- trace 不暴露 raw user text、memory content、provider raw response 或 media body。

可能新增：

- `tests/test_phase1_realtime_loop_deep_gate.py`

## Verification Commands

实施计划最终至少应覆盖：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_call_simulator.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_realtime_call_simulator.py --scenario all --quiet
```

如果新增 Phase 1 deep gate：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py -q
```

## Phase 2 And Phase 3 Dependency Notes

Memory Intelligence 后续应依赖真实 realtime conversation 事件：

- 哪些 turn 适合作为 candidate memory。
- 用户何时确认、拒绝或纠正记忆。
- interrupt/cancel 的半成品内容默认不进入长期记忆。

Skill v1 后续应依赖稳定的 realtime tool path：

- Skill 只映射 tool，不直接执行。
- 工具调用中断和失败必须能在 trace 中复盘。
- Skill 权限要能解释一次 realtime turn 为什么可以调用某个工具。

这两部分暂不展开设计。Phase 1 没稳定前，不开始 Phase 2/3 深化实现。

## Review Checklist

- 本文只覆盖 Phase 1 Realtime Assistant Loop。
- 没有新增 Phase 6 或 Phase 7。
- 没有把 ASR/TTS 放进 `assistant_agent`。
- 没有引入新 Agent loop。
- 没有改变 ToolExecutor 的治理边界。
- 没有把 Gateway 变成大脑。
- 没有要求真实 provider。
