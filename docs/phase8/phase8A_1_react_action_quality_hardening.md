# Phase 8A.1：ReAct Action Quality Hardening

## 1. 背景

Phase 8A Assistant Loop MVP 完成后，系统已经从：

```text
intent-router workflow
```

进入：

```text
assistant_node + ReAct + tools as actions
```

也就是说，DeepSeek 或其他 chat provider 不再只是 `chat_node` 的回答模型，而是中心大脑：

```text
Reason -> Action -> Observation -> Reason -> Final Answer
```

这带来的核心变化是：

```text
以前优化重点：intent router 分类是否正确
现在优化重点：assistant 是否能稳定地产生正确 action
```

Phase 8A.1 的目标不是继续扩图，也不是开始 planning/reflection，而是把 Phase 8A MVP 的 ReAct 行为变得更稳定、更可控、更容易调试。

---

## 2. Phase 8A.1 要解决的问题

Phase 8A MVP 之后可能出现这些问题：

1. LLM 知道要调用工具，但 `tool_name` 不稳定。
2. LLM 生成的 `tool_input` 不符合工具 schema。
3. 工具 observation 太原始，LLM 不知道下一步该怎么总结。
4. LLM 多次调用同一个失败工具，形成循环。
5. LLM 因为描述不清误调用工具。
6. tool_calls 在 trace 中可见，但 action reason 不清楚。
7. Validator 只检查工具存在，不检查 action 是否符合业务边界。
8. 本地 mock/offline 和真实 provider_smoke 的行为边界不够清楚。
9. response_text 只展示最终回答，看不出 assistant 为什么调用某个 action。
10. eval 仍然像旧 router 一样只看 expected_tools，没有检查 ReAct decision quality。

---

## 3. 阶段目标

Phase 8A.1 的目标是强化：

```text
assistant prompt
action spec
tool input schema
AssistantDecision validation
ToolObservation formatting
CapabilityValidator / ActionValidator
LoopGuard
trace explainability
ReAct eval cases
```

一句话：

```text
让 assistant 会正确使用工具，并且系统能拦住错误 action。
```

---

## 4. 不做什么

Phase 8A.1 不做：

```text
不新增复杂 planning
不新增 reflection_node
不新增真实 Provider
不默认调用真实 Provider
不删除旧 conditional graph
不把 assistant_loop 设为默认
不重构 ToolRegistry
不重构 ToolExecutor
不修改 tools/__init__.py
不部署到公网
```

如果发现必须做 planning/reflection，应该记录为 Phase 8B / Phase 8C 后续任务，而不是塞进 Phase 8A.1。

---

## 5. ReAct 运行契约

目标循环：

```text
assistant_node
  -> AssistantDecision
  -> ActionValidator
  -> execute_requested_tool_node
  -> ToolExecutor
  -> ToolObservation
  -> assistant_node
  -> final_answer / ask_followup / next tool_call
```

核心规则：

```text
LLM proposes
Validator checks
ToolExecutor executes
Observation returns
LLM continues
```

不能变成：

```text
LLM directly executes Provider
```

也不能回退到：

```text
keyword router directly selects branch
```

---

## 6. AssistantDecision 强化

`AssistantDecision` 至少应稳定支持：

```text
type
message
tool_name
tool_input
reason
confidence
missing_slots
safety_notes
```

其中：

```text
type = final_answer | tool_call | ask_followup
```

### 6.1 tool_call 要求

当 `type == tool_call` 时：

```text
tool_name 必须存在
tool_name 必须在 action spec 中
tool_input 必须是 dict
reason 应说明为什么要调用该工具
confidence 应可选但推荐存在
```

缺少这些字段时，不应该直接执行工具。

可以转为：

```text
ask_followup
safe final_answer
assistant_decision_parse_failed
invalid_action
```

### 6.2 final_answer 要求

当 `type == final_answer` 时：

```text
message 必须存在
不能再执行工具
可以引用已有 observation
```

### 6.3 ask_followup 要求

当 `type == ask_followup` 时：

```text
message 必须说明缺什么
missing_slots 应列出缺失字段
不能执行工具
```

---

## 7. ActionSpec 强化

assistant_node 看到的工具不应该只是名字列表。

应提供 action spec view：

```text
name
description
input_schema
when_to_use
when_not_to_use
examples
runtime_constraints
provider_constraints
```

### 7.1 image_understanding 示例

```text
name: image_understanding
when_to_use:
  - 用户要求描述、分析、识别图片内容
  - 用户上传图片并问图里有什么
when_not_to_use:
  - 用户要求生成新图片
  - 用户要求创建 3D 场景
required_inputs:
  - image_ref
```

### 7.2 render_3d 示例

```text
name: render_3d
when_to_use:
  - 用户明确要求 3D
  - 用户明确要求渲染
  - 用户明确要求建模
  - 用户明确要求创建场景预览
when_not_to_use:
  - 用户只是要求描述图片或视频里的场景
  - 用户只是问“画面场景是什么”
required_intent:
  - explicit_render_intent
```

这可以防止：

```text
“描述图片里的场景”误触发 render_3d
```

---

## 8. ToolObservation 强化

Observation 不是给人看的原始日志，而是给 assistant_node 做下一步决策的压缩上下文。

建议 observation 包含：

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

### 8.1 成功 observation 示例

```json
{
  "tool_name": "image_understanding",
  "status": "succeeded",
  "summary": "图片中是一双白色运动鞋，背景简洁，材质类似皮革和橡胶。",
  "output_ref": "provider://vision/qwen/...",
  "structured_output": {
    "objects": ["white sneaker"],
    "colors": ["white", "gray"],
    "materials": ["leather", "rubber"]
  },
  "next_step_hint": "User asked for description only; final answer is likely enough.",
  "redacted": true
}
```

### 8.2 失败 observation 示例

```json
{
  "tool_name": "image_generation",
  "status": "failed",
  "summary": "Image generation provider is not configured.",
  "error_code": "provider_unconfigured",
  "error_message": "Image generation is unavailable in local_demo.",
  "next_step_hint": "Explain limitation or ask user to enable provider_smoke.",
  "redacted": true
}
```

---

## 9. ActionValidator / CapabilityValidator

Phase 8A.1 要强化 validator，让它检查：

```text
tool exists
tool enabled
runtime profile allows this action
tool_input schema valid
required media/input exists
action intent matches user request
provider readiness if real provider would be needed
loop guard not exceeded
```

### 9.1 render_3d guard

必须明确：

```text
描述图片/视频里的“场景”不是 render_3d intent
```

不应触发 render_3d：

```text
图里是什么？请简要描述主要物体、颜色、材质和场景。
请描述这张图片的场景。
这个视频里的场景发生了什么？
画面中的主要场景是什么？
分析一下图片中的物体和场景。
```

应触发 render_3d：

```text
根据这张图创建一个 3D 场景预览。
把这个商品放进一个客厅场景里渲染。
生成一个三维商品展示场景。
请用 3D 方式建模这个场景。
渲染一个包含这个商品的展示空间。
```

---

## 10. LoopGuard

必须防止 ReAct 失控。

建议至少实现：

```text
max_tool_iterations
same_tool_failure_limit
unknown_tool_limit
invalid_tool_input_limit
empty_decision_limit
```

默认建议：

```text
max_tool_iterations = 5
same_tool_failure_limit = 1
unknown_tool_limit = 1
invalid_tool_input_limit = 1
empty_decision_limit = 1
```

触发 loop guard 时：

```text
停止工具调用
生成安全 final_answer
trace 记录 loop_guard_triggered
```

---

## 11. Trace explainability

Phase 8A.1 应让 trace 中能看出：

```text
assistant 为什么选择某个 action
decision confidence 是多少
validator 是否通过
tool observation 摘要是什么
是否触发 loop guard
最终回答基于哪些 observations
```

不要求复杂 UI，但 run/trace 数据结构和日志要能支持排查。

不能记录：

```text
API Key
Authorization header
Bearer token
raw Provider response
完整 base64
真实用户私密数据
```

---

## 12. Eval 策略变化

旧 eval 主要看：

```text
expected_intent
expected_tools
response_text
```

Phase 8A.1 应增加 ReAct-specific eval：

```text
assistant_decision_type
assistant_tool_name
decision_reason_present
observation_summary_present
validator_rejection_reason
loop_guard_not_triggered_unless_expected
```

关键不是让 LLM 百分百一样输出，而是检查：

```text
行为稳定
工具选择合理
错误 action 被拦截
不会循环失控
```

---

## 13. 成功标准

Phase 8A.1 完成后应满足：

```text
assistant_loop 模式下 action 选择更稳定
tool_input schema 错误会被拦截
observation 对 assistant 可读
render_3d 不再因“描述场景”误触发
工具失败不会导致循环
trace 能解释 action 选择
default conditional graph 不被破坏
local_demo/offline_eval 仍然不调用真实 Provider
```
