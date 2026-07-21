# 工具调用架构

本文是 `assistant_agent` 工具注册、暴露、调用和执行边界的当前权威说明。源码和测试优先于本文。

## 1. 设计目标

工具系统采用最小安全模式，只在公共运行时保留四个核心机制：

1. `ToolRegistry` 注册和查找工具；
2. 基于 `ToolSpec.category`、toolset、profile、media、env 和显式配置组装本轮工具目录；
3. 使用工具自己的 Pydantic `input_schema` 校验模型参数；
4. 对声明 `requires_confirmation=true` 的工具执行简单、结构化的确认检查。

系统不再维护独立的 `ToolPolicyMetadata`、`ToolPolicyInterpreter`、risk gate、进程内幂等 ledger 或
ToolScheduler。工具执行顺序由 assistant loop 决定，当前 native tool calls 按模型返回顺序处理。

工具目录遵守一个核心不变量：

> 本轮暴露给模型的工具，就是本轮允许模型调用的工具。

因此 category/toolset/profile 负责构造候选空间，`RunToolCatalog.available_tool_names` 是唯一的
run-scoped 可用集合，不再另建 executable allowlist。

## 2. 核心数据契约

### 2.1 ToolSpec

`ToolSpec` 是注册、暴露、provider schema 和执行阶段共同读取的单一工具契约：

```text
name / description
input_schema（完整、规范化的 JSON Schema）
category: read | generate | write | dangerous
toolset
requires_confirmation
requires_env / enabled_by_default / skill_only
requires_media
progress_message
```

工具类直接声明这些字段；Registry 不再维护按工具名索引的 `_TOOL_CONTRACTS` 或
`_ACTION_USAGE` 副本。`description` 同时承担 provider 可见的简短使用说明，避免再造一套
`when_to_use/when_not_to_use/runtime_constraints` 元数据。

`input_schema` 是工具输入描述的唯一事实源，必填字段只由标准 JSON Schema 的 `required` 表达。
`ToolSpec` 不再维护独立的 `required_inputs` 或自定义 `fields` 视图；prompt 若需要压缩，只在渲染时
临时移除 title、截短 description，不改变原始 schema。

这些字段都是工具级静态事实，不根据输入中的 `action` 动态改变安全语义。读写行为明显不同的能力应
拆成不同工具，例如 `memory_retrieval` 与 `memory_save`；它们可以共享 `toolset="memory"`，但保持
不同的 `category`、schema 和确认要求。

未知或未声明分类的本地工具使用保守默认值：`category="dangerous"` 且
`requires_confirmation=true`。

### 2.2 RunToolCatalog

`RunToolCatalog` 是每个 assistant turn 的工具目录快照：

```text
schema_version: run_tool_catalog_v1
available_tool_names
selection_reasons
excluded_reasons
```

`available_tool_names` 同时用于：

- 选择要转换成 OpenAI-compatible schema 的 ToolSpec；
- 记录 context/trace 中本轮可用工具；
- 由 `ActionValidator` 拒绝模型猜测出的未暴露工具名。

目录中不存在 registered、qualified、exposed、executable 四份重复集合。registry inventory 仍可通过
`ToolRegistry.list_specs()` 独立获得；排除原因只用于解释为什么某个已注册工具没有进入本轮目录。

### 2.3 工具前置输入

工具需要的业务前置信息属于该工具的输入契约，不进入通用治理分支：

- 必填字段、类型、范围和非空规则由工具自己的 Pydantic schema 表达；
- OpenAI/MCP adapter 会把 schema 的 required 字段原样转换给 provider；
- 跨字段或领域安全规则由工具自己的 `validate_call()` 表达；
- 缺少前置信息时，模型应先向用户询问，而不是用空值或猜测值调用工具。

例如 `weather.location` 是必填且去除首尾空白后必须非空。用户明确说“明天”或某个日期时，LLM
必须结合 runtime 当前日期生成标准化的 `target_date=YYYY-MM-DD`；用户没有指定日期时才省略，由
adapter 使用运行时当天。`web_fetch.url` 的 URL 格式和访问安全分别由其 schema 与工具/adapter 的
URL 安全边界负责。

### 2.4 统一视觉工具

LLM 只看到一个公共视觉工具 `vision_understanding`。图片理解和视频理解不是两个并列工具，而是
该工具根据 `image_ids`、`video_ids` 或 `video_ref` 选择的内部执行分支：

```text
vision_understanding
  -> image branch
  -> video branch
```

`video_understanding` 只可作为内部 capability/result 标签用于兼容、trace 和结构化结果区分，不能
注册为独立 ToolSpec、进入 `RunToolCatalog.available_tool_names` 或发送给主 LLM。实时视频后台观察、
显式视频上传和图片理解都经过同一个 `ActionValidator -> ToolExecutor -> ToolRegistry ->
vision_understanding` 公共边界；视频 Provider、滚动语义记忆与关键帧 fallback 均封装在内部视频分支。

## 3. 注册与暴露

Provider 运行只有一个全局边界：`MULTIMODAL_AGENT_PROVIDER_MODE=mock|real`。mock 模式强制主
LLM 和所有 Provider-backed tools 使用 mock 实现，即使环境中存在真实 key；real 模式要求主 LLM
完整配置，并且只注册配置完整的真实 Provider 工具，禁止回退到 mock。memory、Python 等纯本地能力
不属于 Provider，不受“真实调用”伪分类。weather、calendar、contacts 等 MCP 能力在 real 模式按实际
MCP mapping 逐个注册，未映射的能力不进入 Registry。

内置工具按能力域由 `tools/builtin_plugins.py` 中的受信任进程内 `ToolPlugin` 构造，再统一注册到
`ToolRegistry`。插件只负责基于结构化配置和已注入依赖创建 Tool 实例；不能直接暴露或执行工具，也
不能绕过 `ActionValidator -> ToolExecutor -> ToolRegistry` 治理链路。默认插件集合是显式、固定顺序的
代码列表，不扫描目录或自动导入第三方模块。MCP 和显式本地 module loader 仍是独立扩展入口。

`ToolRegistry.list_specs()` 和 `get_spec(name)` 复用同一个 builder，因此 provider schema、validator 和
executor 读取的是同一份契约。新增或移除一个内置能力包时，修改对应 `ToolPlugin` 及默认插件列表，
不再向 `create_default_registry()` 添加领域工具的实例化和 Provider readiness 分支。

`visual_image_search` 与 `vision_understanding` 同属 `VisionToolPlugin`，但在 real 模式下仍分别检查各自
Provider 配置。`delegate_to_agent` 不属于默认工具插件；显式 multi-agent 入口需要时在自己的 Registry
上直接注册该工具。

本轮目录由以下链路生成：

```text
ToolRegistry.list_specs()
    -> select_prompt_tool_specs()
    -> RunToolCatalog.available_tool_names
    -> OpenAI-compatible tools schema
    -> ChatAdapter.chat(tool_choice="auto")
```

`tool_choice="auto"` 是 provider API 参数：允许模型在回答文本和调用已提供工具之间选择。它不授予
额外权限；模型只能看到 `RunToolCatalog` 对应的 schema。

目录装配只读取结构化事实，不读取 `request.text` 做关键词/正则意图路由：

- `read` 默认可暴露；
- `generate` 需要代码配置或显式启用，当前内置生成工具由代码配置启用；
- `write` 需要代码配置或显式启用，当前 memory 写入工具由代码配置启用；
- `dangerous` 必须结构化显式启用，并继续满足 env/profile 条件；
- `requires_env`、`requires_media`、`skill_only` 可以排除工具；
- `enabled_tools`、`enabled_toolsets`、`enabled_skills` 等 metadata 是显式结构化 opt-in。

LLM 决定是否调用、调用哪个已暴露工具以及参数内容。category/toolset/profile 只定义候选空间，不替
模型猜测用户意图。

## 4. Provider schema 转换

`schemas/tool_spec_adapters.py` 只给 provider-neutral `ToolSpec.input_schema` 包装协议外壳，不再转换或
合并另一套字段描述：

```json
{
  "type": "function",
  "function": {
    "name": "weather",
    "description": "Look up current or short-range weather for a location.",
    "parameters": {"type": "object", "properties": {}}
  }
}
```

category、确认、profile、env 等系统字段不会发送给模型，也不需要靠“截断系统信息”从
一份混合 JSON 中剥离。adapter 只挑选 provider 协议需要的 name、description 和 input schema。

模型返回的 native `tool_calls` 会归一化为内部 `AssistantDecision`，然后进入统一执行链路。

OpenAI-compatible Chat adapter 从 `ProviderConfig` 读取主调用超时；默认
`MULTIMODAL_AGENT_CHAT_TIMEOUT_SECONDS=75`，应小于入口 turn 总预算。Qwen 混合思考模型默认由
runtime 显式发送 `extra_body.enable_thinking=false`，避免简单工具轮次把大部分延迟消耗在隐藏思考；
只有明确设置 `QWEN_CHAT_ENABLE_THINKING=true` 才启用。该参数只发送给 Qwen，不改变其他 Provider
payload。

## 5. 校验与执行

固定主链路：

```text
native tool_calls
    -> AssistantDecision
    -> ActionValidator
    -> ToolExecutor
    -> ToolRegistry
    -> tool.run()
    -> ToolResult
    -> model observation
    -> 下一轮 LLM
```

### 5.1 ActionValidator

`ActionValidator` 负责执行前的确定性校验：

- 工具已注册；
- 当 state 存在 `RunToolCatalog` 时，工具属于 `available_tool_names`；
- 输入是 JSON object，且通过工具 Pydantic schema；
- `requires_media` 对应的媒体输入存在；
- 工具自己的 `validate_call()` 通过，例如 Python 安全代码检查；
- durable mode 的计划和可信 step 绑定有效。

校验成功时 validator 会保留已构造的 Pydantic input model。主 assistant loop 和 workflow runner 把它
交给 executor，executor 只在该对象上补充可信运行时字段，`tool.run()` 不再重复解析同一份模型输入。
直接调用 executor 的兼容入口没有已验证对象时，工具边界仍执行一次 Pydantic 兜底校验。

durable step 绑定只在 durable task 已启用时生效，用于保证 worker 当前执行的仍是计划中 ready 的
step、工具名与 step 匹配，并且已确认输入的 digest 没有在确认后被替换。普通前台调用不承担这套
检查。

### 5.2 ToolExecutor

`ToolExecutor` 保留运行时闭环需要的职责：

- 绑定可信 `user_id`、`session_id` 和 request-scoped media；
- 若 `ToolSpec.requires_confirmation=true`，检查
  `request.metadata.tool_confirmation={confirmed: true, tool_name: ...}`；
- 预留和结算 provider 调用预算；
- 传播 cancel，read 工具按全局 provider retry policy 重试，非 read 工具不自动重试；
- 写入 tool call history、event 和 trace；
- 默认只向 history/trace 写入安全摘要；本地显式设置
  `MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT=1` 时，统一记录经过 secret sanitizer 的工具输入输出；
- 将结构化 `ToolResult` 转成下一轮模型 observation。

executor 仍采用 `prepare_tool_call -> invoke_tool -> commit_tool_result` 三段形式。该分段用于保证预算、
状态、event 和 trace 的提交顺序，不代表存在独立 scheduler 或并发 policy：

- prepare：绑定运行时输入、检查简单确认、创建 call record、预留预算；
- invoke：只执行工具主体，不修改共享 `AgentState`；
- commit：结算预算并写回 state/history/event/trace。

幂等语义不再由通用 ToolSpec 治理。具体 provider 若需要 idempotency key，应在领域 schema/adapter 或
durable task 协议中处理；通用 executor 不维护进程内重复调用 ledger。

工具不再各自声明 trace 脱敏策略。完整内容开关是本地运行级事实：默认关闭；开启后仍排除
`raw_provider_payload`、`provider_raw_response` 和内联大块数据，并继续执行 secret、base64、绝对路径
和长度清理。real 模式不应开启该变量。

## 6. 确认语义

确认是工具级布尔契约，不根据同一工具输入里的 `action` 动态切换：

- `calendar_search`: `category=read`, `requires_confirmation=false`；
- `calendar_create`: `category=write`, `requires_confirmation=true`；
- `memory_retrieval`: `category=read`, `requires_confirmation=false`；
- `memory_save`: `category=write`, `requires_confirmation=false`，细粒度敏感记忆确认由
  `MemoryManager`/memory write policy 自己负责；
- `memory_media_ingest`: `category=write`, `requires_confirmation=true`。

未确认的通用写工具不会调用 `tool.run()`，而是返回：

```json
{
  "status": "confirmation_required",
  "requires_confirmation": true
}
```

确认 metadata 只能确认同名工具；模型在 tool input 中自行添加 `confirmed=true` 不构成授权。

## 7. MCP、本地工具和 workflow

MCP 定义先经过 server allowlist，再转换成 namespace tool name 和简单 ToolSpec：read-only MCP 工具是
`category=read`；其他 MCP 工具是 `category=write` 且需要确认。注册后的 MCP proxy 走相同
`ActionValidator -> ToolExecutor -> ToolRegistry` 链路。

本地 `@tool` decorator 直接声明 `category`、`requires_confirmation`、`toolset` 和
`requires_media`，不再要求 rich policy 或 per-tool timeout/retry metadata。CLI validate 检查能否生成
合法 ToolSpec；simulate 仍通过 validator/executor 执行。

workflow skill 只能调用已注册且 permission 匹配的工具。read 工具允许按 workflow retry 配置重试；
非 read step 若声明重试，manifest 必须显式声明 idempotency，由 workflow/领域实现承担该语义。

## 8. 代码导航

- `schemas/tools.py`：`ToolSpec`、`RunToolCatalog`、`ToolResult`、`ToolCallRecord`；
- `tools/*.py`：每个工具自身的 schema、description、category 和确认等静态契约；
- `tools/registry.py`：工具注册、查找和 Pydantic schema 提取；
- `services/context/tool_catalog.py`：结构化目录装配；
- `services/context/tool_exposure.py`：category/profile/media 暴露规则；
- `schemas/tool_spec_adapters.py`：OpenAI/MCP schema 转换；
- `agent/action_validator.py`：run catalog、Pydantic、media、durable 校验；
- `agent/tool_executor.py`：身份绑定、简单确认、预算、调用和提交；
- `tests/test_tool_governance.py`：工具治理稳定契约。

## 9. 不变量

- 所有模型驱动工具调用必须经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`；
- 暴露给模型的工具一定可进入执行链路，未暴露工具一定被 validator 拒绝；
- category/toolset/profile/env/media 只基于结构化事实，不从自然语言推断；
- Pydantic schema 是工具参数形状的权威；
- 主模型工具调用链对同一输入只构造一次 Pydantic model；
- 工具级确认只读可信 request metadata，不信任模型输入；
- Memory、MCP、durable task、workflow、CLI 和 Gateway 不绕过统一工具边界；
- 默认测试与 eval 保持 mock/local/offline，真实 provider 必须显式启用。
