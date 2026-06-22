# Assistant Loop Architecture Upgrade

## 1. 一句话目标

把当前 `chat_node` 从 intent-router 工作流里的一个可选分支，升级为中心 `assistant_node`。

所有模型、工具、能力服务都作为 `assistant_node` 可以选择调用的 action。

Phase 8A 只做：

```text
assistant-driven tool loop
```

暂不一步到位做复杂 planning / reflection。

---

## 2. 当前问题

当前架构大致是：

```text
START
  ↓
load_memory
  ↓
detect_intent
  ↓
route_by_intent
  ├─ vision_node
  ├─ search_node
  ├─ compare_node
  ├─ image_generation_node
  ├─ render_node
  ├─ memory_node
  ├─ chat_node
  └─ multi_tool_node → plan_steps
  ↓
compose_response
  ↓
save_memory
  ↓
END
```

这说明当前系统的中心是：

```text
intent router
```

而不是：

```text
assistant brain
```

当前问题：

1. `chat_node` 只是 fallback，不是中心大脑。
2. 能力调用由 intent/router 预先硬分支决定。
3. 每新增复杂能力，都需要扩展 route 分支和特殊规则。
4. 真实用户请求常常混合了聊天、理解、生成、搜索、记忆和追问，单一 intent 很难覆盖。
5. planning / reflection 很难自然接入。
6. 工具失败恢复、多轮观察、追问逻辑容易散落在节点里。

---

## 3. 目标架构

Phase 8A MVP 图结构：

```text
START
  ↓
load_memory
  ↓
assistant_node
  ↓
route_after_assistant
  ├─ execute_tool → assistant_node
  └─ compose_response → save_memory → END
```

核心变化：

```text
旧：
detect_intent → route_by_intent → fixed capability node

新：
assistant_node → decide next action → execute action → observe → assistant_node
```

后续可扩展为：

```text
START
  ↓
load_memory
  ↓
assistant_node
  ↓
route_after_assistant
  ├─ execute_tool    → assistant_node
  ├─ plan_node       → assistant_node
  ├─ reflection_node → assistant_node
  └─ compose_response → save_memory → END
```

---

## 4. 核心原则

### 4.1 保留旧图

不要删除旧的 `conditional_graph.py`。

新增并行图：

```text
src/multimodal_agent/agent/assistant_loop_graph.py
src/multimodal_agent/agent/assistant_loop_nodes.py
```

Runtime 通过 graph mode 选择：

```text
conditional
assistant_loop
```

建议环境变量：

```text
MULTIMODAL_AGENT_GRAPH_MODE=assistant_loop
```

第一版默认仍保持：

```text
conditional
```

等新图测试、demo、eval 稳定后，再考虑把默认切到 `assistant_loop`。

---

### 4.2 assistant_node 只决定，不执行

`assistant_node` 负责读取上下文并输出结构化决策。

它可以读取：

```text
用户输入
image_ref / video_ref / attachments
memory_context
tool_observations
available actions
runtime profile
provider mode
previous assistant messages
```

它输出：

```text
AssistantDecision
```

它不能：

```text
直接调用 Provider SDK
直接调用 adapter
直接写 memory store
直接调用 HTTP tool
直接绕过 ToolExecutor
```

---

### 4.3 所有能力都是 action

Action 包括：

```text
direct_chat
image_understanding
video_understanding
image_generation
product_search
price_compare
render_3d
memory_retrieval
memory_save
ask_followup
final_answer
```

其中：

```text
final_answer
ask_followup
```

可以是 assistant 自身动作。

工具类 action 必须走：

```text
assistant_node
  -> AssistantDecision(type="tool_call")
  -> execute_requested_tool_node
  -> ToolExecutor
  -> ToolRegistry
  -> Tool
  -> Adapter
  -> ToolObservation
  -> assistant_node
```

---

### 4.4 不要重构已有工具体系

Phase 8A 不应该大改：

```text
ToolRegistry
ToolExecutor
ProviderConfig
Provider adapters
EventSink / trace
```

核心改动在：

```text
LangGraph 新图
assistant_node
AssistantDecision schema
ToolObservation schema
execute_requested_tool_node
Runtime graph mode
tests / demo / eval
```

---

## 5. AssistantDecision

建议新增：

```text
src/multimodal_agent/schemas/assistant_decision.py
```

推荐结构：

```python
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class AssistantDecisionType(str, Enum):
    FINAL_ANSWER = "final_answer"
    TOOL_CALL = "tool_call"
    ASK_FOLLOWUP = "ask_followup"
    PLAN = "plan"
    REFLECT = "reflect"


class AssistantDecision(BaseModel):
    type: AssistantDecisionType
    message: str | None = None

    tool_name: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)

    reason: str | None = None
    confidence: float | None = None

    missing_slots: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)
```

Phase 8A 只实现：

```text
final_answer
tool_call
ask_followup
```

`plan` / `reflect` 可以保留枚举，但不实现复杂行为。

---

## 6. ToolObservation

建议新增：

```text
src/multimodal_agent/schemas/tool_observation.py
```

推荐结构：

```python
from typing import Any, Literal
from pydantic import BaseModel, Field


class ToolObservation(BaseModel):
    tool_name: str
    status: Literal["succeeded", "failed", "skipped"]
    summary: str | None = None
    output_ref: str | None = None
    structured_output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
```

作用：

```text
execute_requested_tool_node 执行工具后，把 ToolResult 转成 assistant 可读 observation。
assistant_node 根据 observation 决定继续调用工具、追问或最终回答。
```

---

## 7. AgentState / GraphState 修改

建议增加字段：

```python
assistant_decision: AssistantDecision | None = None
assistant_messages: list[dict[str, Any]] = Field(default_factory=list)
tool_observations: list[ToolObservation] = Field(default_factory=list)
loop_count: int = 0
max_tool_iterations: int = 5
last_action_name: str | None = None
consecutive_tool_failures: int = 0
```

如果当前项目用 TypedDict / Pydantic 混合，应按现有风格实现，但语义保持一致。

---

## 8. assistant_loop_graph

建议新增：

```text
src/multimodal_agent/agent/assistant_loop_graph.py
```

伪代码：

```python
from langgraph.graph import END, START, StateGraph

from multimodal_agent.agent.assistant_loop_nodes import (
    assistant_node,
    compose_assistant_response_node,
    execute_requested_tool_node,
    load_memory_node,
    route_after_assistant,
    save_memory_node,
)
from multimodal_agent.agent.graph_state import AgentGraphState
from multimodal_agent.agent.tracing import trace_graph_node


def build_assistant_loop_graph():
    graph = StateGraph(AgentGraphState)

    graph.add_node("load_memory", trace_graph_node("load_memory", load_memory_node))
    graph.add_node("assistant", trace_graph_node("assistant", assistant_node))
    graph.add_node("execute_tool", trace_graph_node("execute_tool", execute_requested_tool_node))
    graph.add_node("compose_response", trace_graph_node("compose_response", compose_assistant_response_node))
    graph.add_node("save_memory", trace_graph_node("save_memory", save_memory_node))

    graph.add_edge(START, "load_memory")
    graph.add_edge("load_memory", "assistant")

    graph.add_conditional_edges(
        "assistant",
        route_after_assistant,
        {
            "execute_tool": "execute_tool",
            "finish": "compose_response",
        },
    )

    graph.add_edge("execute_tool", "assistant")
    graph.add_edge("compose_response", "save_memory")
    graph.add_edge("save_memory", END)

    return graph.compile()
```

---

## 9. route_after_assistant

建议逻辑：

```python
def route_after_assistant(graph_state):
    state = graph_state["state"]
    decision = state.assistant_decision

    if state.status == "failed":
        return "finish"

    if state.loop_count >= state.max_tool_iterations:
        return "finish"

    if decision is None:
        return "finish"

    if decision.type == "tool_call":
        return "execute_tool"

    return "finish"
```

必须处理：

```text
unknown tool
invalid tool input
tool failure
loop overflow
missing decision
```

---

## 10. execute_requested_tool_node

职责：

```text
读取 AssistantDecision(tool_call)
校验 tool_name
校验 tool 是否存在
校验 runtime profile / graph mode / provider safety
校验 tool_input
调用 ToolExecutor
把结果转成 ToolObservation
追加 state.tool_observations
递增 loop_count
记录 trace / tool_calls
```

---

## 11. assistant_node 决策策略

Phase 8A 可以采用两层策略：

```text
1. deterministic assistant policy
2. real chat_adapter structured decision parser
```

### local_demo / offline_eval

默认使用 deterministic policy，保证：

```text
pytest 可复现
eval 可复现
demo flows 可复现
不调用真实 Provider
```

### provider_smoke / pilot

只有在显式配置真实 chat provider 时，才允许 chat_adapter 产生真实 decision。

LLM 输出必须是结构化 JSON。

解析失败时：

```text
不要崩溃
不要执行工具
返回 ask_followup 或 safe final_answer
记录 assistant_decision_parse_failed
```

---

## 12. compose_response 兼容

新图里 assistant_node 可能已经生成最终回答。

compose_response 的职责变成规范化：

```text
如果 decision.type == final_answer，用 decision.message
如果 decision.type == ask_followup，用 decision.message
如果 loop 超限，用 observations 生成安全总结
如果工具失败，用 errors/observations 生成安全错误回复
写入 AgentResponse
```

可以复用现有 ResponseComposer，但要避免旧 composer 根据固定 intent/capability 分支强行拼接无关工具结果。

---

## 13. Runtime graph mode

建议新增：

```python
class AgentGraphMode(str, Enum):
    CONDITIONAL = "conditional"
    ASSISTANT_LOOP = "assistant_loop"
```

环境变量：

```text
MULTIMODAL_AGENT_GRAPH_MODE=conditional
```

允许值：

```text
conditional
assistant_loop
```

Runtime 初始化：

```python
if config.agent_graph_mode == "assistant_loop":
    self._graph = build_assistant_loop_graph()
else:
    self._graph = build_conditional_agent_graph()
```

第一版默认仍保持：

```text
conditional
```

---

## 14. RuntimeProfile 与 GraphMode 的边界

RuntimeProfile 控制真实 Provider：

```text
local_demo
offline_eval
provider_smoke
pilot
```

GraphMode 控制主控图：

```text
conditional
assistant_loop
```

二者不能混淆。

推荐组合：

| runtime profile | graph mode | 是否允许真实 Provider | 用途 |
|---|---|---|---|
| local_demo | conditional | 否 | 旧稳定默认 |
| local_demo | assistant_loop | 否 | 本地验证新大脑 |
| offline_eval | assistant_loop | 否 | 离线回归 |
| provider_smoke | assistant_loop | 显式允许 | 手动真实模型 smoke |
| pilot | assistant_loop | 显式允许 | 后续小范围试点 |

---

## 15. Phase 8 子阶段

### Phase 8A：Assistant Loop MVP

范围：

```text
assistant_loop_graph
assistant_loop_nodes
AssistantDecision
ToolObservation
graph mode 配置
final_answer / tool_call / ask_followup
execute_tool loop
loop guard
tests / demo / eval
```

不做：

```text
复杂 planning
复杂 reflection
默认切新图
新增真实 Provider
```

### Phase 8B：Planning Follow-up

后续再做。

范围：

```text
plan_node
PlanStep
current_plan
current_step_index
assistant 可选择 plan action
```

注意：Planner 不能重新变成中心 router。Planning 只是 assistant 可调用的内部 action。

### Phase 8C：Reflection Follow-up

后续再做。

范围：

```text
reflection_node
ReflectionResult
tool failure reflection
low confidence reflection
loop limit reflection
```

Reflection 不能直接执行工具，只能：

```text
revise decision
ask_followup
final_answer with caveat
```

---

## 16. Demo / Eval 迁移策略

新增 demo scenarios：

```text
assistant_loop_direct_chat
assistant_loop_image_generation
assistant_loop_image_understanding
assistant_loop_video_understanding
assistant_loop_product_search_compare
assistant_loop_render_explicit
assistant_loop_scene_description_no_render
assistant_loop_memory_followup
```

关键回归：

```text
“描述图片里的场景”不能触发 render_3d
“根据这张图创建 3D 场景预览”可以触发 image_understanding -> render_3d
普通聊天不调用工具
明确生成图片才调用 image_generation
商品搜索 + 比价应调用 product_search -> price_compare
```
