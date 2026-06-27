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
  beta-trial.md
  memory-manager-boundary.md
  planning-and-reflection-roadmap.md

tasks/phase8/
  README.md
  assistant-loop-mvp.md
  planning-followup.md
  reflection-followup.md

prompts/phase8/
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
tasks/phase8/*.md
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
Phase 8B ReAct Plan Mode
  ↓
Phase 8C Reflection Follow-up
```

先只执行 Phase 8A。不要在第一轮同时实现 planning 和 reflection。

## Phase 8B Plan Mode Boundary

Phase 8B 将 planning 定义为同一个 ReAct assistant loop 内部的受控 plan mode，而不是与 ReAct 平行的 `plan_and_solve` 执行策略。

```text
START
  ↓
load_memory
  ↓
assistant_node
  ├─ enter_plan_mode / exit_plan_mode → assistant_node
  ├─ tool_call → execute_tool → assistant_node
  └─ final_answer / ask_followup → compose_response
  ↓
save_memory
  ↓
END
```

Plan mode 只是 ReAct loop 的状态和 action：

```text
plan_mode.active
current_plan
current_step_id
plan_revision_count
plan_status
```

LLM 可以通过结构化 `AssistantDecision` 进入、更新或退出 plan mode；代码只负责 schema、工具白名单、预算、状态、trace 和安全边界。

历史兼容字段 `execution_strategy=plan_and_solve` 只能作为 CLI/Web/API 的 plan-mode hint：runtime 仍进入同一个 assistant loop，并在 prompt 中提示 LLM 优先考虑 `enter_plan_mode`。它不是 graph selector，也不要求维护第二套执行分支。

所有计划内工具执行仍必须走同一条 ReAct 工具路径：

```text
ToolSpec
ActionValidator
ToolExecutor
ToolObservation
ProviderBudget
TraceStore
MemoryManager / MemoryContext
AgentState / AgentResponse contract
```

Plan mode 约束：

```text
assistant_node -> enter_plan_mode -> assistant_node
assistant_node -> tool_call -> ToolObservation -> assistant_node
assistant_node -> exit_plan_mode -> final_answer / ask_followup / continue tool loop
```

离线评估使用 `plan_mode` suite 覆盖进入计划、计划更新、按计划单步工具调用、工具失败后修订计划、退出计划并交付最终回答。该 suite 通过 scripted chat adapter 模拟 LLM 结构化输出，不调用真实 Provider，也不复用旧规则 planner 生成真实路径计划。

不要实现：

```text
独立 plan_and_solve graph / subgraph
execution_strategy router
planner_llm -> 本地 for-loop 自动跑完整 plan -> response composer
把旧规则 planner 伪装成真实 LLM planning
```

## ReAct Trace Visibility Boundary

Phase 8 ReAct 对外只暴露结构化运行过程：

```text
decision reason -> action -> observation -> final_answer
```

其中 `decision reason` 对应 `AssistantDecision.reason` 或 API/Web Console 中的 `decision_summary`，只能是简短、高层、可审计的决策理由。模型内部推理、完整 chain-of-thought、`Thought:` 前缀内容、分析草稿和思维链都不应进入 prompt 输出要求、trace、API 响应或 Web Console 展示。

兼容 parser 可以从 markdown/code fence 或非 JSON 前缀中提取合法 JSON decision，但前缀文本不能被当作公开 trace 字段保存。API 保持兼容字段 `reason` / `decision_summary`，不新增 `thought` 字段。

真实 non-mock chat provider 的推荐工具调用路径是 provider-native tool calling：`ToolSpec` 转成 provider tools schema，模型返回 `tool_calls`，本地系统再转换为 `AssistantDecision` 并执行 validator / executor / observation。`prompt_json` 只作为 mock/offline、显式 fallback 和兼容路径。

专用 memory 工具的 provider-native public schema 不暴露内部 `action` 字段：`memory_retrieval` 只表达检索输入，`memory_save` 只表达保存输入；历史通用 `memory(action=retrieve|save)` 工具保留用于兼容 mock/offline 路径。若旧链路或模型多传 `action`，专用工具会忽略该额外字段，并由工具运行时继续检查 `query` / `content.text` / `content.summary` 等语义必需输入。

真实 ReAct / provider-native 路径中，是否调用 `memory_retrieval` / `memory_save` 是 assistant 的语义决策：普通首次文案、搜索、生成或建议任务应直接处理；只有用户明确引用上次、之前、历史对话、已保存记忆、继续之前任务或“按我的已保存偏好”时才检索记忆。写入记忆同样由 assistant 选择 `memory_save` 作为候选 action，本地 `ActionValidator` / `MemoryWritePolicy` / `MemoryManager` 负责必填字段、敏感信息、去重、profile 更新和审计边界。assistant loop 图尾不再自动把每次运行总结写入长期记忆；mock/offline 和旧 conditional demo 路径可保留规则化 memory 行为用于稳定测试。

记忆归属必须绑定运行时身份：即使 provider-native tool call 参数中包含 `user_id` / `session_id`，本地执行也使用 `UserRequest.user_id` / `UserRequest.session_id` 写入和检索，避免模型生成的默认值（例如 `user_default`）污染其他用户的长期记忆。

### Prompt Tool Catalog Boundary

`prompt_json` 路径可以按用户请求只渲染相关 `ToolSpec`，降低 prompt 噪声和上下文成本。该召回只影响 prompt 文本中的工具说明，不是权限系统：

```text
tool_specs            # 完整工具列表，供 native tool schema、validator、executor 使用
prompt_tool_specs     # prompt_json 渲染用的工具说明子集
tool_catalog_summary  # trace/debug 中的召回数量、选中工具和原因
```

当召回器无法可靠判断相关工具时，必须回退完整 `tool_specs`。provider-native tool calling 仍向 provider 传递完整工具 schema，本地执行也继续经过 `ActionValidator -> ToolExecutor -> ToolRegistry`。

## LangGraph Thread Boundary

本项目将 `session_id` 明确定义为业务会话 thread：

```text
UserRequest.session_id == SessionStore.thread_id == conversation/memory session key
```

当前 LangGraph 图仍是“一次请求一次运行”的执行图，不是可从上一次结束节点继续恢复的多轮对话图。因此 checkpointer 的 `thread_id` 使用 run scoped id，避免同一个 `session_id` 的新请求复用上一次已完成的 graph state：

```python
config = {
    "configurable": {
        "thread_id": state.run_id,
        "session_id": request.session_id,
        "user_id": request.user_id,
        "run_id": state.run_id,
    }
}
```

`SessionStore` 只记录 thread 元数据和索引，例如 title、last_run_id、last_trace_id、run_count。默认使用内存；当 conversation history 配置为 JSONL 时，session index 会落到同目录 `sessions.jsonl`。长期用户记忆继续走 `MemoryManager`。

Web Console 删除会话必须走 `DELETE /sessions/{session_id}?user_id=...`，由 API 先删除 `sessions.jsonl` 中的 session index，再清理 `conversation_history.jsonl` 中同一 `user_id + session_id` 的短期对话。该操作不删除 `long_term_memories.jsonl`；长期记忆通过 Memory Snapshot / audit 的单条 memory delete 治理。

LangGraph checkpointer 接入口已经保留：

```text
LANGGRAPH_CHECKPOINTER_BACKEND=none | memory
```

当前默认 `memory`。Runtime 已把 graph state 拆成：

```text
checkpoint state: 纯数据，允许序列化
runtime context: 工具、模型、store、manager 等运行时依赖
```

运行期依赖通过 `GraphRuntimeContext` 在节点执行时注入，节点返回给 LangGraph 前会移除 `IntentDetector`、`ToolExecutor`、`ChatAdapter`、`MemoryManager`、`TraceStore` 等不可 checkpoint 的对象。后续如果需要跨进程恢复，再在 `services/checkpointer.py` 后面接 SQLite/Postgres checkpointer。

## Memory Boundary

Phase 8 memory 通过 `MemoryManager` 收拢边界：

```text
Agent / Assistant Loop / Memory Tools
  -> MemoryManager
  -> MemoryStore / Retrieval / Context Builder / WritePolicy
  -> InMemoryStore / JsonlMemoryStore / future DB or external memory service
```

当前 `MemoryManager` 负责加载分层 context、显式记忆保存、重复显式记忆合并、用户画像 `user_profile` 更新和 completed-run task summary 保存。用户画像暂时复用 `memory_type=preference`、`source=user_profile`，避免新增类型破坏现有检索/排序合同。

Graph state、memory tools、memory audit API 和 beta 用户数据删除都只依赖 `MemoryManager`；`MemoryStore` 保留为 runtime 内部构造细节和底层持久化接口。

长期记忆对用户展示时按认知层收敛：

```text
semantic  语义记忆：稳定偏好、事实和用户画像
episodic  情景记忆：一次任务或经历的摘要
artifact  产物引用：商品、图片、视频、渲染、生成结果等对象引用
```

底层 `memory_type` 继续保留 `preference/task/product/image/video/generation/render/artifact/conversation` 等结构化类型；`source` 只表示产生方式，例如 `explicit_user_request`、`user_profile`、`agent_task_summary`。

显式“记住 X”写入规则：

```text
保存 1 条原始语义记忆 explicit_user_request
可以更新 1 条派生用户画像 user_profile
纯 memory_save run 不再额外写 agent_task_summary
缺少真实 text/summary 时拒绝写入，不落“用户显式保存了一条记忆”占位摘要
```

检索质量门控：当 `MemoryQuery.query` 非空时，本地检索优先只返回关键词/中文短语片段命中的记忆；具体实体或主题没有命中时返回空结果，不再 fallback 到用户全部历史，避免把无关 task summary 注入 prompt。只有明确承接型 query（例如“继续”“上次”“这个风格”）允许使用最近记忆 fallback。空 query 用于浏览/审计时仍按用户列出最近记忆。

Embedding / 向量检索不是当前默认依赖。后续应作为可选 adapter 接到 `MemoryManager` / `MemoryStore` 后面，测试默认继续使用本地 deterministic 行为，真实 embedding provider 只通过显式配置启用。

### Conversation History

多轮对话历史属于 session-scoped context，不等同于长期 `MemoryStore`。默认是进程内存储；当 `MULTIMODAL_AGENT_MEMORY_BACKEND=jsonl` 且未显式配置 conversation backend 时，会自动使用同目录 JSONL：

```text
MULTIMODAL_AGENT_CONVERSATION_HISTORY_BACKEND=memory | jsonl
MULTIMODAL_AGENT_CONVERSATION_HISTORY_PATH=.local/memory/conversation_history.jsonl
MULTIMODAL_AGENT_MAX_CONVERSATION_HISTORY_TURNS=8
```

本地 JSONL 的相对路径按仓库根目录解析，不按启动命令的当前工作目录解析。

这解决“同一 session 重启服务后丢失最近对话上下文”的本地开发问题。长期偏好仍应通过显式记忆保存进入 `MemoryManager`，默认 JSONL 文件名为 `.local/memory/long_term_memories.jsonl`，避免把完整聊天流水误当成用户画像。该 JSONL 只包含 `MemoryItem` 数据行；本地文件说明放在 `.local/memory/readme_memory.md`，不混入数据文件。

### Memory Audit

Memory 审计先通过结构化 API 和薄 CLI 完成，而不是引入 Claude Code 风格 notebook：

```text
GET    /memory/users/{user_id}/items
GET    /memory/users/{user_id}/items/{memory_id}
GET    /memory/users/{user_id}/audit
GET    /memory/users/{user_id}/snapshot?session_id=...&query=...
DELETE /memory/users/{user_id}/items/{memory_id}
DELETE /memory/users/{user_id}/sessions/{session_id}
```

`snapshot` 是面向调试和学习的只读视图，用来同时观察：

```text
SessionStore thread index
ConversationHistory recent turns
MemoryManager retrieved layered context
MemoryAudit summary
Runtime storage boundary names
```

它不把 conversation history 写进长期记忆，也不把 LangGraph checkpointer 当作用户记忆；默认只展示摘要和 prompt-safe memory item，只有显式 `include_content=true` 时才返回已通过 `MemoryItem` 校验/脱敏的 content。

Web Console 右侧 Memory Snapshot 使用同一组 API 做治理视图：短期对话来自 conversation history，长期记忆来自本次 query 召回的 layered memory context，技术信息折叠展示 storage/checkpoint/thread 边界。单条长期记忆删除先在面板内确认，再调用 `DELETE /memory/users/{user_id}/items/{memory_id}` 并刷新 snapshot，不提供批量清空入口。

CLI：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/memory_audit.py --server http://127.0.0.1:8000 list --user-id demo_user
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/memory_audit.py --server http://127.0.0.1:8000 audit --user-id demo_user
```
