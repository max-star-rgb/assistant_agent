# Phase 8A.1 Task：ReAct Action Quality Hardening

## Goal

在 Phase 8A Assistant Loop MVP 完成后，强化 ReAct assistant loop 的 action 质量。

重点不是新增能力，而是让 `assistant_node` 更稳定地选择 action，让系统能拦住错误 action，并让 observation / trace 对后续决策和调试更有用。

## Read first

```text
AGENTS.md
docs/phase8/README.md
docs/phase8/assistant-loop-architecture-upgrade.md
docs/phase8/phase8A_1_react_action_quality_hardening.md
tasks/phase8/assistant-loop-mvp.md
src/multimodal_agent/agent/assistant_loop_graph.py
src/multimodal_agent/agent/assistant_loop_nodes.py
src/multimodal_agent/schemas/assistant_decision.py
src/multimodal_agent/schemas/tool_observation.py
src/multimodal_agent/tools/
src/multimodal_agent/adapters/
tests/
```

如果某些 Phase 8A MVP 文件不存在，先停止并说明 Phase 8A MVP 尚未完成，不要直接补做 Phase 8A MVP。

## Preconditions

必须已经完成：

```text
Phase 8A Assistant Loop MVP
```

也就是项目中应已有：

```text
assistant_loop_graph
assistant_loop_nodes
AssistantDecision
ToolObservation
MULTIMODAL_AGENT_GRAPH_MODE=assistant_loop
basic assistant_node -> execute_tool -> assistant_node loop
```

## Scope

本任务允许修改：

```text
assistant_loop_nodes
AssistantDecision validation
ToolObservation formatting
ActionSpec / available action view
CapabilityValidator / ActionValidator
LoopGuard
trace/run event fields
assistant_loop routing tests
demo/eval cases
docs
```

本任务不应大改：

```text
ToolRegistry
ToolExecutor
ProviderConfig
Provider adapters
conditional_graph
```

## Requirements

### 1. ActionSpec / available action view

让 `assistant_node` 看到的 action 信息不只是工具名。

至少应包含：

```text
name
description
input_schema 或 required_inputs
when_to_use
when_not_to_use
runtime_constraints
```

可以先用轻量结构实现，不要求完整 JSON Schema introspection。

### 2. AssistantDecision validation

强化 `AssistantDecision` 校验：

- `tool_call` 必须有合法 `tool_name`。
- `tool_call` 的 `tool_input` 必须是 dict。
- unknown tool 不能执行。
- invalid tool_input 不能执行。
- parse failed 不能执行工具。
- `final_answer` 必须有 message。
- `ask_followup` 必须有 message。

### 3. ToolObservation formatting

把工具结果转换成对 assistant 有用的 observation。

Observation 应包含：

```text
tool_name
status
summary
output_ref
structured_output
error_code
error_message
next_step_hint
redacted
```

如果现有 schema 不适合完全加入所有字段，可以按项目风格增加等价字段，但必须保留语义。

### 4. ActionValidator / CapabilityValidator

在 `execute_requested_tool_node` 执行前进行 action 校验。

至少检查：

```text
tool exists
tool enabled
runtime profile allows action
required inputs exist
tool_input shape is valid
explicit render intent if tool is render_3d
```

### 5. render_3d negative guard

必须修复：

```text
“描述图片/视频里的场景”不应触发 render_3d
```

不应触发 render_3d 的 case：

```text
图里是什么？请简要描述主要物体、颜色、材质和场景。
请描述这张图片的场景。
这个视频里的场景发生了什么？
画面中的主要场景是什么？
分析一下图片中的物体和场景。
```

应触发 render_3d 的 case：

```text
根据这张图创建一个 3D 场景预览。
把这个商品放进一个客厅场景里渲染。
生成一个三维商品展示场景。
请用 3D 方式建模这个场景。
渲染一个包含这个商品的展示空间。
```

### 6. LoopGuard

实现或强化：

```text
max_tool_iterations
same_tool_failure_limit
unknown_tool_limit
invalid_tool_input_limit
empty_decision_limit
```

触发 guard 时：

```text
停止工具调用
生成安全 final_answer
trace 记录 loop_guard_triggered
```

### 7. Trace explainability

trace/run history 中应能看到：

```text
assistant decision type
selected tool
decision reason
confidence if present
validator result
validator rejection reason
observation summary
loop guard event
```

不要记录：

```text
API Key
Authorization header
Bearer token
raw Provider response
完整 base64
真实用户私密数据
```

### 8. Eval / tests

新增或更新 assistant-loop 专用测试。

至少覆盖：

- direct chat -> final_answer，不调用工具。
- image description -> image_understanding，不调用 render_3d。
- explicit 3D render -> render_3d。
- unknown tool 被 validator 拒绝。
- invalid tool_input 被 validator 拒绝。
- tool failure 生成 observation。
- same tool failure 不会无限循环。
- max_tool_iterations 生效。
- action reason 出现在 trace 或 decision 中。
- local_demo / offline_eval 不调用真实 Provider。
- conditional graph 默认行为不破坏。

## Out of Scope

不要做：

```text
复杂 planning
reflection_node
新增真实 Provider
真实 API smoke
默认切 assistant_loop
删除 conditional graph
大规模重构 ToolRegistry
大规模重构 ToolExecutor
修改 tools/__init__.py
公网部署
```

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
git status --short
```

如果存在专门的 assistant loop / routing eval 命令，也一并运行。

## Stop condition

完成 Phase 8A.1 后停止。

不要自动开始 Phase 8B planning。
不要自动开始 Phase 8C reflection。

最终回复应包含：

```text
Phase 8A.1 ReAct Action Quality Hardening complete.

Summary:
- ActionSpec:
- AssistantDecision validation:
- ToolObservation:
- Validator:
- LoopGuard:
- Trace explainability:
- Tests run:
- Remaining risks:
- Recommended next step:
```
