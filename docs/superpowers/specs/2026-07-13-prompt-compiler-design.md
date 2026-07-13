# PromptCompiler 统一生产提示词编译设计

日期：2026-07-13

## 1. 背景

当前项目已经具备可用的 Context Compiler v1：`AssistantContextPack` 负责承载请求、会话、记忆、实时任务状态、计划、工具 observation、工具 schema 和 capability descriptor，`SystemPromptPolicy` 负责系统提示词 profile，context renderer 负责把上下文渲染为 provider 消息。

但生产 `ChatRequest` 的最终组装仍分散在三条路径：

- `assistant_loop_nodes.py` 的 provider-native decision 路径；
- `assistant_loop_nodes.py` 的 tool-limit final-only summary 路径；
- `runtime.py` 的 native runtime、realtime phone 和 final-only 路径。

这些路径分别拼接 system、user、assistant tool-call 和 tool-result 消息，并各自处理工具 schema、fallback tool-call payload 和生成参数。继续扩展 persona、spoken style、完整 payload 预算或 prompt 版本时，分散组装会增加行为漂移风险。

本设计引入一个窄职责、无副作用的 `PromptCompiler`，把生产 provider 请求的最终组装统一到单一入口。

## 2. 已确认的设计决策

- 第一阶段只做生产路径收敛，不接入 persona 或 spoken-style Markdown。
- 第一阶段是严格等价重构，不改变模型可见提示词、工具集合、消息顺序、fallback payload 或生成参数。
- 覆盖 provider-native decision、assistant-loop final-only summary、native runtime、realtime phone 和 native final-only handoff。
- legacy `render_prompt_json_context` 保持独立，仅继续服务离线兼容和现有测试。
- 使用单一 `PromptCompiler` 服务门面，不使用纯函数散列流水线，也不引入按 profile 注册的 strategy registry。
- 设计文档不单独提交；按仓库 `AGENTS.md` 要求，与对应代码、测试和文档更新统一提交。

## 3. 目标

1. 所有生产 native prompt 路径通过唯一入口构造完整 `ChatRequest`。
2. 保持现有 `TEXT_DEFAULT`、`REALTIME_PHONE` 和 `FINAL_ONLY` 行为不变。
3. 保持 context、memory、tool policy、profile trust 和 trace 的既有所有权。
4. 为后续 profile 组合、受控 persona Markdown、完整 payload token 预算和 prompt observability 提供稳定编译边界。
5. 使用 characterization、单元和集成测试证明重构前后请求等价。

## 4. 非目标

本阶段不做以下工作：

- 不修改任何 system prompt 文案或 profile 组合方式；
- 不新增 persona、spoken style 或其他 Markdown prompt source；
- 不改变工具候选集选择算法；
- 不改变 memory 读取、写入、排序或注入策略；
- 不改变 context 字符预算、token 估算或 compaction；
- 不修改 provider overflow retry 语义；
- 不移除或重构 legacy prompt-json renderer；
- 不新增 prompt module registry、场景分类器或新的 Agent 调度层；
- 不新增统一外部错误码或 `PromptCompilationError` 包装层。

## 5. 架构

新增文件：

```text
src/assistant_agent/services/context/prompt_compiler.py
```

统一后的调用关系：

```text
assistant_loop_nodes / AgentGraphRuntime
  ├─ 解析可信 SystemPromptProfile 与 SystemPromptOptions
  ├─ build_traced_assistant_context_pack(...)
  ├─ PromptCompiler.compile(...)
  │    ├─ render_system_instruction(...)
  │    ├─ render_native_tool_context(...) / render_final_only_context(...)
  │    ├─ 构造 assistant tool-call / tool-result 消息对
  │    ├─ 选择 context pack 已提供的 prompt tool schemas
  │    └─ 构造 ChatRequest
  ├─ build_context_report(...)
  └─ ChatAdapter.chat(...) / stream_chat(...)
```

### 5.1 调用方职责

`assistant_loop_nodes` 和 `AgentGraphRuntime` 继续负责：

- 从可信入口配置解析 profile 和 options；
- 构建 `AssistantContextPack`；
- 管理 ReAct 迭代和 provider 调用；
- 管理 context overflow 压缩与 retry-once；
- 记录 context report、trace、usage 和 provider 结果；
- 将各自现有的空 user query fallback 和 tool-call ID 前缀显式传给 compiler。

调用方不再手工拼接 provider-native system、user、assistant tool-call 或 tool-result 消息。

### 5.2 Context Builder 职责

现有 Context Builder 保持不变，继续负责：

- 收集 request、conversation、session summary、memory、realtime task state、plan 和 observations；
- observation prompt-safe compaction；
- context budget 和 compaction；
- prompt tool subset 和 capability descriptor；
- 生成 `AssistantContextPack` 和相关构建报告。

Context Builder 不生成最终 provider `ChatRequest`。

### 5.3 PromptCompiler 职责

`PromptCompiler` 只负责确定性编译：

- 渲染当前 profile 的 system instruction；
- 渲染 native user context；
- 把已有 native calls 和 observations 组装为消息对；
- 把已选 ToolSpec 转换为 provider-native tool schema；
- 设置现有 tool choice 和生成参数；
- 返回完整 `ChatRequest` 及 report 所需的编译材料。

`PromptCompiler` 不读取数据库或文件，不检索 memory，不访问 ToolRegistry，不选择 profile，不调用工具或 Provider，不写 metadata，不发 trace。

### 5.4 SystemPromptPolicy 与 renderer

`SystemPromptPolicy` 和现有 renderer 继续作为纯渲染依赖。第一阶段不重组公共规则，也不改变 `REALTIME_PHONE` 的独立提示词内容。

### 5.5 ContextReport

`build_context_report` 继续由调用方执行。runtime 在编译结果仍处于当前调用栈时，直接使用 compiler 产出的 system instruction 和 selected tool specs。assistant-loop 的现有延迟 trace 汇总不保存完整编译结果，只复用 compiler 暴露的确定性 `prompt_tool_specs_for_mode` helper，并继续通过相同 system policy 渲染报告输入，避免给 graph state 增加仅供 trace 使用的完整 prompt 对象。compiler 不产生 trace 或审计副作用。

## 6. 内部契约

契约仅供仓库内部服务使用，不作为 HTTP、WebSocket 或包级公共 API，因此放在 `prompt_compiler.py` 中并使用不可变 dataclass，不在 `assistant_agent.__init__` 聚合导出。

```python
@dataclass(frozen=True)
class PromptCompileRequest:
    user_id: str
    session_id: str
    mode: PromptCompileMode
    user_query_fallback: str
    profile: SystemPromptProfile
    options: SystemPromptOptions
    context_pack: AssistantContextPack
    observations: tuple[dict[str, Any], ...]
    native_calls: tuple[dict[str, Any], ...]
    tool_call_id_prefix: str
    stream_callback: ChatStreamCallback | None = None
    temperature: float = 0.2
    max_tokens: int = 1024


@dataclass(frozen=True)
class PromptCompileResult:
    chat_request: ChatRequest
    system_instruction: str
    rendered_context: RenderedAssistantContext
    selected_tool_specs: tuple[ToolSpec, ...]
```

`PromptCompileMode` 明确表达三种当前生产编译语义：

```python
class PromptCompileMode(StrEnum):
    NATIVE_TOOL = "native_tool"
    NATIVE_FINAL_ONLY = "native_final_only"
    SUMMARY_FINAL_ONLY = "summary_final_only"
```

profile 继续控制 system instruction；mode 只控制 user context renderer、tool-call message、provider tools、tool choice 和 `ChatRequest.user_query` 的既有差异。二者不合并成一个隐式 profile registry。

`user_query_fallback` 用于 native modes 的空请求兼容，避免 compiler 根据调用点做隐式分支：

- assistant-loop native decision 保持 `"native_tools assistant turn"`；
- native runtime 保持 `"native runtime assistant turn"`；
- native final-only handoff 保持 `"native runtime final answer"`。

`SUMMARY_FINAL_ONLY` 的 `ChatRequest.user_query` 与 user message 都使用 `render_final_only_context` 产出的 final-only prompt，与当前 assistant-loop 行为一致；该模式不使用 `user_query_fallback` 生成 provider query。

`tool_call_id_prefix` 同样显式保留当前兼容差异：

- assistant-loop native decision 使用 `"call_"`；
- native runtime 和 final-only 使用 `"native_runtime_call_"`。

这些兼容字段不是新的业务配置，也不从用户 metadata 读取。

## 7. 编译数据流

`PromptCompiler.compile` 按以下固定顺序执行：

1. 使用传入的 profile 和 options 调用 `render_system_instruction`。
2. `NATIVE_TOOL` 与 `NATIVE_FINAL_ONLY` 调用 `render_native_tool_context`；`SUMMARY_FINAL_ONLY` 调用 `render_final_only_context`。
3. 初始化 system、user 两条消息。
4. native modes 按 observation 顺序追加 assistant tool-call 和 tool-result 消息对；summary final-only 不追加 native tool 消息。
5. `NATIVE_TOOL` 从 `context_pack.prompt_tool_specs` 读取已选工具，为空时回退 `context_pack.tool_specs`；两种 final-only mode 使用空工具集。
6. 根据 mode 确定 provider tools、tool choice 和 user query。
7. 使用传入的 user/session、stream callback、temperature 和 max tokens 构造 `ChatRequest`。
8. 返回 `PromptCompileResult`。

消息顺序固定为：

```text
system
user
assistant tool_call #1
tool result #1
assistant tool_call #2
tool result #2
...
```

tool result 的 observation 继续使用 `json.dumps(observation, ensure_ascii=False)` 序列化。

## 8. Compilation Mode 与 Profile 行为

| Mode | Profile | User context / query | Tool messages | Provider tools | tool_choice |
| --- | --- | --- | --- | --- | --- |
| `NATIVE_TOOL` | `TEXT_DEFAULT` | native context / 当前请求或调用方 fallback | 追加 | 已筛选 ToolSpec | `auto` |
| `NATIVE_TOOL` | `REALTIME_PHONE` | native context / 当前请求或调用方 fallback | 追加 | 已筛选 ToolSpec | `auto` |
| `NATIVE_FINAL_ONLY` | `FINAL_ONLY` | native context / 当前请求或调用方 fallback | 追加 | `[]` | `none` |
| `SUMMARY_FINAL_ONLY` | `FINAL_ONLY` | final-only prompt / final-only prompt | 不追加 | `[]` | `None` |

第一阶段同时保留两种现有 final-only 行为：native runtime handoff 继续使用 native context 和 `tool_choice="none"`；assistant-loop tool-limit summary 继续使用 `render_final_only_prompt` 的 JSON final-answer contract，且 `tool_choice` 保持 `None`。`render_final_only_prompt` 是当前生产 summary renderer，不归类为 legacy prompt-json decision renderer。

如果任一 final-only mode 的 context pack 意外携带工具，compiler 仍强制输出 `tools=[]`。这避免 final-only 由错误的上游工具集合重新获得调用能力。

## 9. Tool-call 消息兼容

compiler 把当前重复的 tool-call payload 组装逻辑收敛为一个内部纯函数。行为保持：

- 优先使用 native call 的 raw payload；
- 保留 raw payload 中已有的 `id`、`type` 和 `function` 字段；
- 缺少 raw ID 时使用 `tool_call_id_prefix + one_based_index`；
- 工具名按 raw function name、native call name、observation tool name、`"unknown"` 的现有优先关系补齐；
- raw arguments 是非空字符串时原样保留；
- 否则把结构化 arguments 使用 `ensure_ascii=False` 编码为 JSON；
- native calls 少于 observations 时，仍为剩余 observation 构造 fallback payload；
- 每个 assistant tool-call 后必须紧跟对应 `tool` message，且 `tool_call_id`、name 保持匹配。

## 10. 错误处理

第一阶段不新增静默恢复或异常包装：

- system/context 渲染异常原样向调用方传播；
- observation JSON 序列化失败保持当前异常语义；
- `ChatRequest` Pydantic 校验异常保持原类型；
- compiler 失败时不回退 legacy prompt-json；
- compiler 不捕获异常并生成不完整请求；
- 不新增 `PromptCompilationError`，避免改变调用方现有失败类型。

现有外层治理保持不变：

- ToolRegistry 描述失败仍由 runtime 生成现有结构化 Agent 错误；
- context pack 构建和压缩失败仍属于现有 context/runtime 边界；
- Provider context overflow 仍触发重新构建 context pack 并再次调用 compiler，最多重试一次；
- Provider 调用、工具执行和 memory 失败不属于 compiler 错误处理范围。

## 11. 迁移设计

迁移按以下顺序进行：

1. 为当前三条生产请求构造路径建立 characterization 测试，保存重构前的实际 `ChatRequest`。
2. 新增 compiler 单元测试，先证明当前行为契约。
3. 实现 `PromptCompiler` 和内部 tool-call payload builder。
4. 保留现有私有函数签名，使它们暂时成为 compiler 的薄适配器，减少调用面和测试面变化。
5. 将 `assistant_loop_nodes` native decision 和 summary final-only 路径切换到 compiler。
6. 将 `AgentGraphRuntime` native、realtime phone 和 final-only 路径切换到 compiler。
7. 让 context report 直接使用 `PromptCompileResult` 中的 system instruction 和 selected tool specs。
8. characterization 对比通过后，删除两处重复的消息拼装和 arguments JSON helper。
9. 保留 legacy prompt-json renderer 和对应测试不变。

迁移过程中不允许 compiler 与旧手工拼装同时参与同一次 Provider 请求。

## 12. 测试设计

### 12.1 Characterization 测试

重构前锁定以下输出：

- assistant-loop native decision；
- assistant-loop summary final-only；
- runtime text default；
- runtime realtime phone；
- runtime final-only；
- 多个 tool call/result；
- native call 缺失时的 fallback payload；
- prompt tool subset；
- selector full-list fallback。

结构对比使用：

```python
chat_request.model_dump(exclude={"stream_callback"})
```

`stream_callback` 另行使用对象身份断言。

### 12.2 PromptCompiler 单元测试

新增：

```text
tests/test_prompt_compiler.py
```

覆盖：

- 三种 profile 的 system instruction 与现状一致；
- 三种 compilation mode 的 renderer、user query、tool messages 和 tool choice 与现状一致；
- user message 内容和 section 顺序一致；
- assistant/tool 消息严格成对且顺序一致；
- raw native tool-call 字段得到保留；
- fallback call ID 使用调用方提供的前缀；
- selected tool schema 不重复出现在 native user message；
- 两种 final-only mode 永远不暴露工具；
- 空 user text 使用调用方提供的现有 fallback；
- compiler 不修改 context pack、observations 或 native calls；
- 相同输入的连续编译结果一致；
- stream callback 原样传递。

### 12.3 Runtime 集成测试

使用 scripted/fake real chat adapter 捕获实际 `ChatRequest`，不调用真实 Provider。覆盖：

- assistant-loop native decision 经 compiler 构造；
- assistant-loop tool-limit summary 经 compiler 构造；
- AgentGraphRuntime 经 compiler 构造；
- realtime phone 保持当前 profile；
- tool-limit handoff 使用 final-only profile；
- overflow retry 重新调用 compiler，且只重试一次；
- `context_report_v1` 的 system/tool schema 统计与 compiler 实际输出一致。

### 12.4 静态收敛检查

独立 direct-chat adapter 和测试 fixture 仍可按其职责构造 `ChatRequest`。但生产 native prompt 路径中，compiler 之外不再允许手工拼接完整的 system/user/assistant-tool/tool-result 消息序列。

## 13. 验收标准

实施完成必须同时满足：

- 新旧 `ChatRequest` characterization 结果完全一致；
- 三种 profile 和两种 final-only user context 的提示词正文不变；
- 工具集合、消息顺序、fallback ID 和生成参数不变；
- legacy prompt-json 代码和测试行为不变；
- context report schema 和统计语义不变；
- 新增 compiler 单元与集成测试通过；
- 相关现有 targeted tests 通过；
- `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q` 通过；
- `git diff --check -- AGENTS.md docs src tests .codex/skills` 通过；
- `docs/CONTEXT_ENGINEERING_STATUS.md` 更新为生产 native `ChatRequest` 已统一经 `PromptCompiler` 编译。

## 14. 预计修改范围

新增：

- `src/assistant_agent/services/context/prompt_compiler.py`
- `tests/test_prompt_compiler.py`

修改：

- `src/assistant_agent/agent/assistant_loop_nodes.py`
- `src/assistant_agent/agent/runtime.py`
- 与 native request characterization、system prompt policy、final-only handoff 相关的现有测试
- `docs/CONTEXT_ENGINEERING_STATUS.md`

第一阶段不修改 `system_prompt_policy.py` 和 legacy renderer 的模型可见内容。若为了导入 compiler 需要调整 import，只允许无行为变化的机械调整。

## 15. 后续阶段

本设计验收后，后续工作按独立设计推进：

1. 把 system profiles 重构为公共治理不变量加场景增量；
2. 为 persona 和 spoken style 增加受控、限长、不可授权工具的 Markdown source；
3. 在 compiler 边界统计完整 provider payload token，包括 system、messages 和 tool schemas；
4. 增加 prompt schema version、module/source hash 和 realtime TTFT 评测。

这些工作均不属于本阶段实现范围。
