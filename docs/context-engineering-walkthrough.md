# 上下文工程走读

最后更新：2026-06-29

这份文档面向项目负责人。它解释当前上下文工程服务如何工作、为什么存在、和记忆服务的边界在哪里。它不是历史阶段计划；当前状态入口仍是 `docs/CONTEXT_ENGINEERING_STATUS.md`。

## 一句话理解

上下文工程负责把一次助手决策需要看的材料组装成安全、受预算控制的上下文。

它会处理：

- 当前用户请求。
- 当前会话的摘要和最近对话。
- 记忆服务取回的长期记忆摘要。
- 计划模式状态。
- 已执行工具的观察结果。
- 当前可用工具的 ToolSpec。
- 字符预算、可选 token 报告、压缩原因和追踪/调试摘要。

它不负责：

- 长期记忆存储。
- 记忆检索排序。
- 记忆写入策略。
- 用户画像合并。
- 记忆审计和删除。
- 真实供应商的直接调用。

这些属于记忆服务、供应商适配器、`ToolExecutor` 或 Agent 运行时的边界。

## 请求生命周期

当前主路径可以按下面理解：

```text
UserRequest
  -> run_assistant_request / AgentGraphRuntime
  -> 注入会话对话上下文
  -> load_memory node
  -> MemoryManager.load_into_state(...)
  -> assistant_node
  -> build_assistant_context_pack(...)
  -> 渲染 prompt/native context
  -> 助手决策
  -> ToolExecutor / 最终回答 / 追问
  -> 写入追踪上下文摘要
  -> 保存对话 turn 和会话摘要
  -> 仅在策略允许时调用 MemoryManager.save_from_run(...)
```

关键点：

- 对话上下文和记忆上下文是两条来源不同的数据流。
- `context_summary` 是会话范围的短期摘要。
- 长期记忆只能通过记忆服务写入。
- 助手/LLM 可以提出 `memory_save` 动作或产生候选，但本地策略决定是否真的持久化。

## 主要组件

| 组件 | 文件 | 职责 |
| --- | --- | --- |
| 上下文 schema | `src/multimodal_agent/schemas/context.py` | 定义 `AssistantContextPack`、`ContextPolicy`、`ContextSummary`、`ContextBudgetReport`。 |
| 上下文构建器 | `src/multimodal_agent/services/context/builder.py` | 每轮助手决策前组装上下文包，计算预算，触发压缩，执行预算裁剪。 |
| 对话格式化器 | `src/multimodal_agent/services/context/conversation.py` | 把会话 turn 格式化成最近原文 + 较早摘要。 |
| 压缩触发策略 | `src/multimodal_agent/services/context/policy.py` | 根据预算、工具观察结果、供应商上下文溢出、显式 `/compact` 判断是否压缩。 |
| 上下文摘要器 | `src/multimodal_agent/services/context/compactor.py` | 生成会话范围的 `context_summary`；默认使用确定性实现，真实 LLM 仅在显式供应商运行 profile 下启用。 |
| 工具观察裁剪器 | `src/multimodal_agent/services/context/compaction.py` | 工具结果进入 prompt 前裁剪 raw payload、base64、长命令输出和大列表。 |
| 渲染器 | `src/multimodal_agent/services/context/renderer.py` | 把上下文包渲染成 prompt-json、原生工具调用的 user message 或 final-only prompt。 |
| 助手循环接入点 | `src/multimodal_agent/agent/assistant_loop_nodes.py` | 调用上下文构建器，把渲染后的上下文交给助手，并写追踪上下文摘要。 |

## AssistantContextPack

`AssistantContextPack` 是理解这个系统的中心对象。它不是长期存储对象，而是一次助手决策前的上下文快照。

主要字段：

| 字段 | 来源 | 含义 |
| --- | --- | --- |
| `request` | 当前 `UserRequest` | 用户当前输入和 metadata。 |
| `context_summary` | request metadata / 摘要器 | 当前会话的压缩摘要，不是长期记忆。 |
| `conversation_text` | 对话存储 metadata | 最近对话原文和较早对话短摘要。 |
| `memory_text` | `MemoryManager.load_into_state(...)` | 长期记忆检索后形成的 prompt-safe 文本。 |
| `memory_blocks` | 记忆上下文 metadata | 分层记忆上下文，供 prompt/API/debug 使用。 |
| `plan_state` | `AgentState` | 当前计划模式状态。 |
| `observations` | tool observations | 已执行工具结果的助手可见副本，会被裁剪。 |
| `tool_specs` | `ToolRegistry` | 当前运行时可用工具全集。 |
| `prompt_tool_specs` | 上下文工具目录 | 进入 prompt 的工具子集。 |
| `budget` | 上下文构建器 | 字符预算、可选 token 报告、压缩阶段和原因。 |
| `source_counts` | 上下文构建器 | 调试用来源计数。 |

如果你只看一个对象，就看 `AssistantContextPack`。

## 数据从哪里来

### 1. 当前请求

当前请求来自 API、CLI、WebSocket 或本地运行器。它进入运行时后会被写入 `AgentState.request`。

上下文构建器直接读取：

- `request.text`
- `request.image_ids`
- `request.video_ids`
- `request.metadata`

### 2. 会话对话上下文

会话对话由 `ConversationStore` 管理。当前有内存和 JSONL 两种实现。

进入运行时前，服务会把历史对话注入 request metadata：

- `conversation_context_text`
- `conversation_context_recent_turns`
- `conversation_context_compacted_turns`
- `conversation_context_compacted`
- `session_context_summary`

默认最近 2 轮保留原文，较早对话压缩为短摘要。

`reset_conversation=True` 会同时清空普通 turn 和会话摘要。

### 3. 会话上下文摘要

`context_summary` 是当前会话的压缩上下文。

它包含：

- `task_state`
- `user_constraints`
- `decisions`
- `open_todos`
- `important_refs`
- `dropped_context_note`
- `source_turn_count`

它可以继续注入当前会话，但不进入长期记忆。

### 4. 长期记忆上下文

长期记忆由 `MemoryManager` 加载。上下文工程只消费结果，不负责检索和写入策略。

`MemoryManager` 写入 request metadata：

- `memory_context_text`
- `memory_context_summaries`
- `memory_context_refs`
- `memory_context_blocks`

上下文构建器把这些字段放进 `AssistantContextPack.memory_text` 和 `memory_blocks`。

### 5. 工具观察结果

工具结果会先变成 `ToolObservation`，再进入助手循环的 `tool_observations`。

进入 prompt 前，`compact_observations_for_context(...)` 会生成安全副本：

- 保留摘要、状态、错误、output refs。
- 商品搜索/比价保留标题、价格、URL、平台、可用性等必要字段。
- 列表默认最多保留 3 条。
- 字符串默认最多 1200 字。
- 命令输出默认最多 20 行、1200 字符。
- 原始 provider/file/media payload、base64/data URI、raw HTML/body 不进入 prompt 副本。

原始观察结果不被修改。

## 什么时候会压缩

`CompactionPolicy` 会在这些情况下触发：

- context usage ratio >= 0.80。
- total chars 超过 `max_context_chars`。
- 工具观察结果原始内容超过 `max_tool_result_chars`。
- request metadata 出现供应商上下文溢出。
- 用户显式 `/compact` 或 `compact_context=True`。

默认 `ContextPolicy`：

```text
max_context_chars = 12000
compact_at_ratio = 0.80
hard_compact_at_ratio = 0.92
keep_recent_turns = 2
max_tool_result_chars = 1200
max_memory_context_chars = 500
```

当前控制路径仍以字符预算为准。token-aware 字段已经有报告能力，但默认不改变裁剪和压缩决策。

## 压缩后发生什么

如果触发压缩：

1. 上下文构建器选择摘要器。
2. 默认使用 `DeterministicContextCompactor`。
3. 只有在 `provider_smoke` 或 `pilot` profile 且 chat adapter 不是 mock 时，才使用 `LLMCompactor`。
4. 摘要器输出结构化 `ContextSummary`。
5. 上下文构建器把摘要写回 request metadata：
   - `context_summary`
   - `context_summary_text`
   - `context_summary_present`
   - `context_compactor_type`
6. 后续渲染器把它作为“当前会话摘要”注入 prompt。

重要边界：

- 这不是 `MemoryManager` 写入。
- 这不是长期记忆。
- 这不会调用 `MemoryManager.save_explicit(...)`。

## 预算裁剪顺序

如果最终上下文仍然超过预算，上下文构建器会按顺序裁剪：

1. 记忆文本。
2. 对话文本。
3. 工具观察结果。

工具观察结果通常最后裁剪，因为它是回答和下一步工具调用的证据来源。

裁剪结果会记录在 `ContextBudgetReport`：

- `over_budget`
- `trimmed_chars`
- `trimmed_sections`
- `compression_stage`
- `compression_reasons`
- `context_usage_ratio`
- `compaction_triggered`

## 渲染器做什么

渲染器只负责把 `AssistantContextPack` 转成助手可读的上下文文本。

当前三种渲染：

- `render_prompt_json_context(...)`：prompt-json 决策模式，包含工具 spec 和决策 JSON contract。
- `render_native_tool_context(...)`：供应商原生工具调用模式，只渲染 user-message context，不重复工具 schema。
- `render_final_only_prompt(...)`: 工具调用次数接近上限时，禁止继续调用工具，只要求最终回答。

所有渲染器都明确标注：

- 对话是上下文数据，不是系统指令。
- 记忆是用户历史数据，不是系统指令。
- 工具观察结果和工具输出是数据，不是系统指令。

## Trace 和 API 看到什么

助手决策 trace 会包含带版本的上下文调试摘要：

```text
context_schema_version = context_observability_v1
budget
source_counts
compaction
tool_catalog
compactor_type
context_summary_present
memory_promotion_candidates
memory_promotion_written
```

查询入口：

- `GET /runs/{run_id}`
- `GET /traces/{trace_id}`
- `GET /runs/{run_id}/tool-calls`

trace/API 只暴露已脱敏摘要，不暴露：

- 原始 provider response。
- API key / token / Authorization header。
- base64 或 data URI media payload。
- raw file/media payload。
- sensitive local paths。

## 三个容易混淆的东西

| 概念 | 归属 | 生命周期 | 是否持久化 |
| --- | --- | --- | --- |
| `context_summary` | 上下文工程 / `ConversationStore` | 当前会话的压缩摘要 | 否 |
| `MemoryPromotionCandidate` | 记忆服务 / 写入策略 | 候选长期记忆，默认只审计不写入 | 否，除非策略允许 |
| 长期记忆 | `MemoryManager` / `MemoryStore` | 经过策略和校验的长期记忆 | 是 |

判断规则：

- 当前会话继续用的是 `context_summary`。
- 可能值得记住但还没写的是 `MemoryPromotionCandidate`。
- 真正以后还能检索的是长期记忆。

## 真实例子：用户连续对话

假设用户连续说：

1. “帮我找适合通勤的耳机。”
2. “价格别太高，最好有降噪。”
3. “继续，帮我比较一下。”

系统会这样处理：

1. `ConversationStore` 保存前两轮 turn。
2. 第三轮开始前，最近 turn 被注入 `conversation_context_text`。
3. `MemoryManager` 根据第三轮请求决定是否加载长期记忆。
4. `assistant_node` 调用 `build_assistant_context_pack(...)`。
5. 如果历史太长，旧 turn 会进入 `context_summary`。
6. 如果 `product_search` 观察结果很大，prompt 副本只保留少量商品字段和 URL。
7. 渲染器生成 prompt，助手基于已有观察结果选择 `price_compare` 或 `final_answer`。
8. trace 记录预算和压缩摘要。
9. 只有用户明确“记住我的偏好”或策略允许，才会写长期记忆。

## 排错入口

### 怀疑上下文没有带上

看：

- `/runs/{run_id}` 的 `context.source_counts`
- `/traces/{trace_id}` 的助手决策事件
- `request.metadata["conversation_context_text"]`
- `request.metadata["memory_context_text"]`

相关测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_assistant_context_renderer.py tests/test_shared_assistant_run_service.py -q
```

### 怀疑压缩没有触发

看：

- `context.budget.context_usage_ratio`
- `context.budget.compaction_triggered`
- `context.budget.compression_stage`
- `context.budget.compression_reasons`
- `context.compactor_type`

相关测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_conversation_context_compaction.py tests/test_assistant_context_renderer.py -q
```

### 怀疑 raw payload 泄漏

看：

- 渲染后的 prompt 中是否有 base64 或原始 provider payload。
- `/traces/{trace_id}` 是否只出现已脱敏摘要。
- 工具观察结果里的 `compaction.pruned_keys` 和 `context.compaction.pruned_payload_keys`。

相关测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_assistant_context_renderer.py tests/test_trace_query_api.py tests/test_run_summary_query.py -q
```

### 怀疑长期记忆被自动写入

先区分：

- `context_summary` 不是长期记忆。
- `memory_promotion_candidate_audit` 不是长期记忆写入。
- `memory_promotion_written > 0` 才表示 promotion 真的写入了。

相关测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_write_policy.py tests/test_memory_manager.py tests/test_memory_tool_boundary.py -q
```

## 常见误解

### 误解 1：摘要就是记忆

不是。`context_summary` 是会话范围的上下文。它服务当前会话恢复，不进入 `MemoryStore`。

### 误解 2：LLM 决定什么时候压缩

不是。压缩触发由 `CompactionPolicy` 决定。`LLMCompactor` 只在显式 provider profile 下负责语义摘要内容。

### 误解 3：token 预算已经控制上下文

还没有。token-aware 当前是报告层。默认裁剪和压缩仍使用字符预算。

### 误解 4：MemoryManager 负责 prompt 渲染

不是。`MemoryManager` 负责记忆上下文的产生。prompt 渲染是上下文工程的职责。

### 误解 5：工具观察结果的原始数据被删掉了

不是。上下文工程只裁剪进入 prompt 的副本。原始工具结果和观察结果合约不应被 prompt pruning 改写。

## 读代码的推荐顺序

1. `src/multimodal_agent/schemas/context.py`
2. `src/multimodal_agent/services/context/builder.py`
3. `src/multimodal_agent/services/context/policy.py`
4. `src/multimodal_agent/services/context/compaction.py`
5. `src/multimodal_agent/services/context/compactor.py`
6. `src/multimodal_agent/services/context/renderer.py`
7. `src/multimodal_agent/agent/assistant_loop_nodes.py`
8. `src/multimodal_agent/services/assistant_run_service.py`
9. `src/multimodal_agent/memory/manager.py`

如果你只想理解架构，不用一开始读阶段计划。

## 修改时的安全清单

改上下文相关代码前，确认：

- 默认 mock/local/offline 不变。
- 不因为检测到 API key 自动启用真实 provider。
- 原始 provider payload、base64、raw media/file 不进入 prompt、trace 或 memory。
- `context_summary` 不写长期记忆。
- 记忆检索、排序、写入策略不进上下文构建器。
- prompt 渲染不进 `MemoryManager`。
- 助手/LLM 不直接决定持久化记忆写入。
- 相关测试和 `docs/CONTEXT_ENGINEERING_STATUS.md` 同步更新。
