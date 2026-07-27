# 工具调用架构

本文是 `assistant_agent` 工具注册、暴露、调用和执行边界的当前权威说明。源码和测试优先于本文。

## 1. 设计目标

工具系统采用最小安全模式，只在公共运行时保留三个核心机制：

1. `ToolRegistry` 注册和查找工具；
2. 基于 `ToolSpec.category`、profile、media、env 和显式工具配置组装本轮工具目录；
3. 使用工具自己的 Pydantic `input_schema` 校验模型参数；

系统不再维护独立的 `ToolPolicyMetadata`、`ToolPolicyInterpreter`、risk gate、进程内幂等 ledger 或
ToolScheduler。工具执行顺序由 assistant loop 决定，当前 native tool calls 按模型返回顺序处理。

工具目录遵守一个核心不变量：

> 本轮暴露给模型的工具，就是本轮允许模型调用的工具。

因此 category/profile 负责构造候选空间，`RunToolCatalog.available_tool_names` 是唯一的
run-scoped 可用集合，不再另建 executable allowlist。

## 2. 核心数据契约

### 2.1 ToolSpec

`ToolSpec` 是注册、暴露、provider schema 和执行阶段共同读取的单一工具契约：

```text
name / description
input_schema（包含模型需要理解和填写的语义参数，不含 runtime-owned 参数）
category: read | generate | write | dangerous
enabled_by_default
requires_media
```

工具类直接声明这些字段；Registry 不再维护按工具名索引的 `_TOOL_CONTRACTS` 或
`_ACTION_USAGE` 副本。`description` 同时承担 provider 可见的简短使用说明，避免再造一套
`when_to_use/when_not_to_use/runtime_constraints` 元数据。
仓库自有工具直接引用唯一的规范 `*Request` / `*Result` 契约，不为同一 Pydantic 模型保留
`*Input = *Request` 或旧 Tool 类名等无转换兼容别名。
仓库自有的内置 ToolSpec 及其模型可见参数 `description` 统一使用中文；工具名、字段名、
枚举值、协议标识符和代码符号保持英文。外部 MCP 与配置插件的描述属于上游契约，不由
runtime 猜测翻译或改写。

`shopping_search` 的短 description 保留三条必要边界：只表达想要某物时先询问；明确要求推荐、
查价、比价或购买链接时调用；工具只搜索和比较，不能下单。展示模板等执行后说明只随 observation
提供，不重复塞进每轮工具 schema。
购物结果遵循标准 ReAct 闭环：`shopping_search` 返回结构化 `ToolResult`，runtime 将其转换为
tool observation，下一轮 LLM 消费商品、报价、链接和展示模板后生成最终文本。系统不注册额外的展示
工具，也不在 Realtime/Gateway 用 presenter 覆盖模型回复；是否输出 `<detail>` 以及选择哪些合格商品
由 LLM 根据 observation 决定，代码只负责工具治理、上下文传递和原样交付最终回答。
本地 Langfuse 会按 observation index 展示 assistant loop 产生的完整 `ToolObservation`，不会再次把它
压缩成 summary/output ref；具体开发观测边界见 `docs/observability-harness.md`。

系统不维护中心 Tool manifest。`tools/ids.py` 只保存已经成为跨层协议的稳定字符串，不枚举
Tool、不参与注册或暴露，新插件内使用的 Tool 默认无需加入。旧 planner/intent 所需的 action、alias
与 capability 映射隔离在 `runtime/legacy_tool_mapping.py`，不能作为新增 Tool 的登记入口。

工具类的 Pydantic `input_schema` 是执行期完整输入和校验的事实源；Registry 生成的
`ToolSpec.input_schema` 保留必填参数及会实质改变业务结果的语义型可选参数，必填字段由标准
JSON Schema 的 `required` 表达。
`ToolSpec` 不再维护独立的 `required_inputs` 或自定义 `fields` 视图；prompt 若需要压缩，只在渲染时
临时移除 title、截短 description，不改变原始 schema。工具通过 `runtime_input_bindings` 声明
runtime-owned 输入；绑定字段不进入
`ToolSpec.input_schema`，因此不会发送给模型。绑定来源只使用结构化执行事实：
`constant`、`runtime_identity`、`request`、`memory_context`、`latest_tool_result` 和
`durable_idempotency`。有 Pydantic 默认值、但不应由模型填写的工具内部参数使用
`model_hidden_input_fields`；它们不需要 runtime binding，执行时由 Pydantic 补齐默认值。

Tool 注册时会检查绑定字段存在、没有重复且静态默认值符合目标字段类型。模型若自行提交 runtime-owned
字段，`ActionValidator` 统一返回 `runtime_owned_tool_input`；内部 observer/worker 如需提供帧引用等
可信动态值，必须使用 `ToolExecutor.runtime_input`，该通道也只能覆盖已声明的 runtime-owned 字段。

这些字段都是工具级静态事实，不根据输入中的 `action` 动态改变安全语义。长期记忆不是工具：
主模型不看到 `memory_search`、`memory_get` 或 `memory_save`；Mem0 recall/capture 是 runtime
生命周期。

未知或未声明分类的本地工具使用保守默认值：`category="dangerous"`。

Tool 系统只保留职责明确且不可互相替代的边界：

- `category` 只表达副作用与安全等级；
- Plugin 只表达代码所有权、Provider、依赖和生命周期；
- Tool name 用于每轮精确启用和执行授权；
- Skill 只表达如何组合已治理工具的工作流。

系统不维护独立 toolset 或业务分类树。部署级批量启停使用 Plugin，单轮暴露使用精确 Tool name
或显式 Skill；Plugin 被装配不代表其 write/dangerous Tool 自动获得授权，Tool 自己的 category、
输入校验仍然生效。

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
`ToolRegistry.list_specs()` 独立获得；排除原因只用于解释为什么某个已注册工具没有通过本轮 category、
media、profile、默认启用或显式授权治理。

### 2.3 工具前置输入

工具需要的业务前置信息属于该工具的输入契约，不进入通用治理分支：

- 必填字段、类型、范围和非空规则由工具自己的 Pydantic schema 表达；
- OpenAI/MCP adapter 会把 schema 的 required 字段原样转换给 provider；
- 跨字段或领域安全规则由工具自己的 `validate_call()` 表达；
- 缺少前置信息时，模型应先向用户询问，而不是用空值或猜测值调用工具。

例如 `weather.location` 是必填且去除首尾空白后必须非空；缺少地点时模型必须先追问，不得猜测或
调用工具。模型侧只额外看到可选 `target_date`：单日使用 `YYYY-MM-DD`，连续多日使用
`YYYY-MM-DD/YYYY-MM-DD` 闭区间，工具内部从该范围派生起止日期和天数，并返回范围内每天的天气。
`web_fetch.url` 的 URL 格式和访问安全分别由其 Pydantic schema 与工具/adapter 的 URL 安全边界负责。

`image_generation.prompt` 是唯一必填的模型输入，商品信息不能替代生成提示词。尺寸、数量、风格、
负向提示词和随机种子等参数只在用户明确指定时由模型传入；未指定时由工具和 Provider adapter 使用
各自默认值。

模型输入遵循最小语义参数原则：

- 用户目标中不可可靠推导的业务语义，例如地点、搜索词、URL、代码、事件标题和开始时间，仍由模型
  按用户输入填写；
- provider、adapter、units、limit、timeout、输出格式等静态技术参数由 Tool 实例的 `constant`
  binding 分配；
- user/session identity、请求媒体、memory context、前序工具结果和 durable 幂等键只在每次执行时
  绑定，不能固化到进程级复用的 Tool 实例，避免并发请求串数据；
- runtime binding 后再次通过工具 Pydantic schema 校验，再进入 `tool.run()`。

### 2.4 统一视觉工具

LLM 只看到一个公共视觉工具 `vision_understanding`，模型侧仅有可选 `question`；当前请求中的
`image_ids`、`video_ids` 或内部 `video_ref` 由 runtime binding 注入。图片理解和视频理解不是两个
并列工具，而是该工具根据绑定后的媒体引用选择内部执行分支：

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
不属于 Provider，不受“真实调用”伪分类。weather、contacts 等 MCP 能力在 real 模式按实际
MCP mapping 逐个注册，未映射的能力不进入 Registry。开发阶段的稳定
`calendar_search` / `calendar_create` 默认使用本地 SQLite，不调用日历 MCP。

`web_search` 与 `web_fetch` 随 `run_server` 创建的 runtime 作为进程内 Web Tool 插件装配，不要求
额外启动本地 relay。`MULTIMODAL_AGENT_SEARCH_PROVIDER=tavily` 时，插件使用 `TAVILY_API_KEY`
直接构造 Tavily Search/Extract adapter；配置不完整则两个 Tool 都不注册。`http` 仍保留为部署方显式
提供通用 HTTP backend 的兼容 Provider，两种真实 Provider 都不得互相静默 fallback。

本地文本文件通过内置 `local_file_access` Plugin 的 `file_read` 工具读取。该工具只接受相对于
`MULTIMODAL_AGENT_FILE_ACCESS_ROOT` 的白名单文本文件路径，默认根目录为 `.data/files`；绝对路径、
隐藏路径、目录穿越、越界 symlink、非普通文件、超限文件和非 UTF-8 内容均拒绝。单次读取上限由
runtime constant binding 注入，长文件通过结果中的 `next_cursor` 分页；工具只返回受控文本，
内容理解和总结仍由主 assistant loop 完成。

邮件只读能力由独立 `email_access` Plugin 装配，不归入
`personal_assistant_mcp`，也不在顶层 `providers/` 新增仅供该 Plugin 使用的专用 adapter。该 Plugin 暴露稳定
`email_search` / `email_read`，内部私有 backend 将它们映射到显式配置的 Workspace MCP
`search_gmail_messages` / `get_gmail_messages_content_batch`。邮件正文 observation 标记为
`untrusted_external_content` 和 `do_not_execute`，只作为主模型分析证据；首版不注册发送、草稿、
标签修改或附件下载工具。

进程内 Tool 插件采用 L2 启动时可插拔协议。每个插件声明
`ToolPluginDescriptor(plugin_id, plugin_version, api_version="tool_plugin_v1")`，并通过
`build_tools(context)` 构造 Tool。Plugin 是独立的启动期装配机制，不是 Tool 契约的子类型；整个
Tool 子系统统一位于 `tools/`，其中内核协议、参数绑定和 Registry 保留在根层，Plugin 框架位于
`tools/plugins/`，内置装配单元位于
`tools/plugins/builtin/<assembly_boundary>/`。`defaults.py` 只保留受信任内置插件的显式清单，不扫描
目录；Tool 内核不反向拥有 Plugin，只有 composition root 知道具体内置 Plugin。
外部 Plugin contract 只有 `assistant_agent.tools.plugins.contracts` 一个规范导入路径；旧
`assistant_agent.tool_plugins` 兼容命名空间已删除。

部署方可以通过逗号分隔的 `MULTIMODAL_AGENT_TOOL_PLUGIN_MODULES` 显式列出可信 Python module；每个
module 必须导出单个 `__assistant_tool_plugin__`。未配置 module 不会被 import，配置错误、协议不兼容、
重复 plugin id、重复 Tool name 或构造失败都会使启动 fail closed。import Python module 等同于执行进程内
代码，因此该入口只适用于 operator 信任的部署代码，不是不可信插件沙箱；不可信或跨进程能力应使用
MCP/独立服务边界。

内置和配置插件经过相同的“发现、构造、全量校验、原子提交”流程。`ToolRegistry` 保存每个 Tool 的
plugin/source/version ownership；默认 runtime 装配结束后 Registry 会 seal，并根据安全契约生成稳定
generation。运行期间不能继续 `register()`，配置变化需要重启生效；当前不支持 unload、replace、drain
或热更新。MCP 仍是外部 Tool source，但其 allowlist proxy 与进程内 Tool 一起在最终 Registry seal 前
提交。

插件只负责基于结构化配置和已注入依赖创建 Tool；不能直接暴露或执行工具，也不能绕过
`ActionValidator -> ToolExecutor -> ToolRegistry` 治理链路。插件加载不等于本轮授权，write/dangerous
Tool 仍默认不暴露，必须由宿主配置或每轮结构化显式 opt-in。`tools.loader` 的 `__assistant_tools__` 保留给
本地 Skill/CLI 入口，不会自动并入默认 runtime 插件协议。

Plugin 按共享 Provider、配置、依赖和生命周期划分，不按 Tool 数量或宽泛业务标签机械拆分。目录名与
`plugin_id` 应表达独立装配边界，避免 `core`、`misc` 等兜底分类。共享同一 MCP mapping、runner 和
adapter bundle 的 weather/calendar/contacts 归属 `personal_assistant_mcp`；配置与 readiness 独立的
`email_access`、`vision_understanding`、`visual_image_search` 分别装配；本地 Python 执行独立归属
`python_execution`。新增已有 Plugin 内的普通 Tool 只修改该 Plugin 目录及其测试；
新增内置 Plugin 额外在 `defaults.py` 可信清单登记一次；新增外部 Plugin 只增加独立 module 和部署
配置。普通 Tool 的增删不得要求修改 Registry、Executor、Validator、assistant loop、Prompt/Context
编译或中心 Tool name 表。只有引入新的宿主级共享基础设施时，才扩展 `ToolPluginContext` 和
composition root。

可用 `python -m assistant_agent.tools.cli plugins` 只读查看启动装配结果、ownership、issue、seal 状态和
generation；该命令不会执行 Tool。`--module` 可重复传入并覆盖环境 module 列表用于部署前验证。
`scripts/run_server.py` 则在服务 lifespan 初始化真实 runtime 后，按 Registry 已保存的 `plugin_id`
输出该 runtime 最终 Registry 的工具名；该展示不修改 ToolSpec 或工具暴露语义，不输出 description、
source、seal 状态或 generation，也不会为了展示而重复装配插件。

`ToolRegistry.list_specs()` 和 `get_spec(name)` 复用同一个 builder，因此 provider schema、validator 和
executor 读取的是同一份契约。新增或移除一个内置能力包时，只增删对应插件目录及 `defaults.py`
可信清单；新增普通外部插件只需提供 module 导出并修改部署配置，无需修改 Registry、Executor、
`tool_ids.py` 或中心 Tool name 表。修改已有插件内的 Tool 时只改该插件目录，不再向
`create_default_registry()` 添加领域工具的实现、实例化或 Provider readiness 分支。`ToolRegistry`
本身只保留在 `tools/registry.py`；默认 Plugin、MCP proxy 和 realtime observer 的启动装配位于
`tools/plugins/registry_factory.py`，避免工具内核反向导入具体内置 Plugin。

weather、calendar、contacts 的 mock adapter 和 MCP backend 是
`personal_assistant_mcp` 的私有实现，分别位于该 Plugin 的 `adapters.py`、`backend.py`；顶层
`providers/` 不保存仅由该 Plugin 消费的 Provider adapter。只有出现跨 Plugin、跨入口的真实复用或
独立应用生命周期时，能力实现才提升为共享 Provider 能力。同样，image generation、shopping、visual image
search、web access 和 Python execution 的单一 owner backend/sandbox 均保留在对应内置 Plugin；
共享治理分别归属 `providers/`、`context/`、`runtime/`、`observability/`、`automation/` 和 `media/`。

普通 real Runtime 使用 `LocalSQLiteCalendarAdapter` 承载稳定的
`calendar_search` / `calendar_create`，数据库默认位于 `.data/calendar/events.sqlite3`；
Langfuse `real_system` Eval 通过 composition root 注入独立数据库，默认位于
`.data/evals/langfuse/calendar.sqlite3`。两者都从 `ToolContext.user_id` 派生 namespace，避免用户间
串数据；Trace 负责执行审计，SQLite 负责可检索业务状态。Google Calendar MCP mapping 暂时不被这两个
稳定工具调用。

`visual_image_search` 与 `vision_understanding` 虽然同属视觉业务域，但 Provider 配置、readiness 和
启停生命周期不同，因此分别归属 `VisualImageSearchPlugin` 与 `VisionUnderstandingPlugin`。原
`delegate_to_agent` 工具已暂时删除；multi-agent 路由与通信服务保留，但不会向任何 Registry 暴露
delegation tool。

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

可信 Agent-Service profile 不维护业务工具名 allowlist，只证明请求来自该入口。所有已注册且
`category=read` 的工具按统一 exposure policy 默认进入候选集合，再应用 `requires_media` 等结构化
条件；generate、write 与 dangerous 工具仍分别要求代码配置、宿主配置或显式启用。profile 不替代
插件 readiness：real 模式的 Provider-backed 工具配置不完整时不会注册，禁止回退 mock。

目录装配只读取结构化事实，不读取 `request.text` 做关键词/正则意图路由：

- `read` 默认可暴露；
- `generate` 需要代码配置或显式启用，当前内置生成工具由代码配置启用；
- `write` 需要代码配置或显式启用，当前 memory 写入工具由代码配置启用；
- `dangerous` 必须结构化显式启用，工具执行阶段仍负责自身环境和安全校验；
- `requires_media` 可以基于入口携带的结构化媒体事实排除工具；
- `enabled_by_default` 保留给 MCP 等需要区分“已注册”和“默认暴露”的能力；
- `enabled_tools`、`enabled_skills` 等 metadata 是显式结构化 opt-in。

LLM 决定是否调用、调用哪个已暴露工具以及参数内容。category/profile 只定义候选空间，不替
模型猜测用户意图。

## 4. Provider schema 转换

`tools/spec_adapters.py` 给 provider-neutral `ToolSpec.input_schema` 包装 OpenAI-compatible
与 MCP 协议外壳。两种模型可见 Schema 都以帮助模型理解和构造参数为目标，
保留参数结构、`type`、`description`、`required` 以及必要的 `enum`；递归移除 Pydantic 自动生成的
`title`、执行期 `default`、根 schema `description`、`additionalProperties`、空 `required`，以及
`minLength`、`minimum`、`maximum`、`pattern`、`format` 等执行期校验约束。模型不会被视为可靠的
Schema 校验器，所有非空、长度、范围、格式和跨字段规则仍由 `ActionValidator` 与工具 Pydantic
schema 确定性执行：

```json
{
  "type": "function",
  "function": {
    "name": "weather",
    "description": "查询指定地点的当前或短期天气；用户指定日期时传 YYYY-MM-DD。",
    "parameters": {"type": "object", "properties": {}}
  }
}
```

category、profile、env 等系统字段不会发送给模型，也不需要靠“截断系统信息”从
一份混合 JSON 中剥离。adapter 只挑选 provider 协议需要的 name、description 和 input schema。

所有仓库内置工具的模型可见参数都应提供简短、明确的中文 `description`；只写 Pydantic 类型或
校验约束而不解释字段语义，不视为完整工具契约。`shopping_search` 向模型暴露必填 `query` 以及
会实质改变检索结果的可选 `budget_min`、`budget_max`；商品特征、使用场景和指定平台写入
`query`，用户明确给出的预算写入对应结构化字段。平台过滤列表和候选数量由 Pydantic 默认值补齐。
购物工具不读取前序视觉结果；需要看图购物时，LLM 先调用
`vision_understanding`，消费其 observation 后自行构造 `shopping_search.query`。购物请求不携带
未使用的身份、memory context 或假想 Provider 兼容字段。`weather`
只暴露 `location` 和 `target_date`，日期范围解析与公制单位由
工具内部固定。`vision_understanding.question` 等没有必填要求但会改变任务结果的
语义型可选参数仍应暴露；当前媒体引用、用户原始请求、身份、采样参数和 rolling context 均来自
runtime。`web_fetch` 只暴露必填 `url`，读取上限和内容格式由 Tool 静态默认值分配。

目录不做 Tool Search、Schema 预算触发的渐进披露或基于请求文本的工具预选。所有通过 category、
media、profile、默认启用和显式 Tool/Skill 治理的 ToolSpec 都直接发送给 Provider；工具规模由部署时
安装的 Plugin、MCP allowlist 和入口 `allowed_tools` 控制。Context report 继续记录 Tool Schema 的实际
字符和 token 占用，只有真实 Provider 失败、延迟或选择质量证据出现后才重新设计大目录方案。

模型返回的 native `tool_calls` 会归一化为严格的内部 `AssistantToolCall`，然后进入统一执行链路。
assistant turn 的内部输出只允许非空 `AssistantTextOutput` 或 `AssistantToolCall`；计划提交通过显式
`task_plan_submit` 工具完成，不再扩展 assistant 输出协议。

OpenAI-compatible Chat adapter 从 `ProviderConfig` 读取主调用超时；默认
`MULTIMODAL_AGENT_CHAT_TIMEOUT_SECONDS=75`，应小于入口 turn 总预算。Qwen 混合思考模型默认由
runtime 显式发送 `extra_body.enable_thinking=false`，避免简单工具轮次把大部分延迟消耗在隐藏思考；
只有明确设置 `QWEN_CHAT_ENABLE_THINKING=true` 才启用。该参数只发送给 Qwen，不改变其他 Provider
payload。

## 5. 校验与执行

固定主链路：

```text
native tool_calls
    -> AssistantToolCall
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
step，且工具名与 step 匹配。普通前台调用不承担这套检查。

### 5.2 ToolExecutor

`ToolExecutor` 保留运行时闭环需要的职责：

- 绑定可信 `user_id`、`session_id` 和 request-scoped media；
- 传播 cancel，read 工具按全局 provider retry policy 重试，非 read 工具不自动重试；
- 写入运行态 tool call state、event 和 trace；持久化工具调用查询统一从 trace 派生；
- 默认只向 trace 写入安全摘要；本地显式设置
  `MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT=1` 时，统一记录经过 secret sanitizer 的工具输入输出；
- `audit_payload` 与 `raw_data_ref` 只通过同一 trace 脱敏边界保留安全投影，不再写入独立工具历史；
- 将结构化 `ToolResult` 转成下一轮模型 observation。

executor 对外只保留串行 `run_tool()`：依次绑定运行时输入、创建 call record、调用
Registry，并按同一调用栈写回 state/event/trace。系统不保留未被生产路径使用的 prepare/invoke/commit
分段接口、PreparedToolCall 或 invocation 中间结果。

`ToolResult.success=false` 只表示一次工具执行失败，不天然等于整个 Agent run 失败。Executor 通过通用、
结构化的 `failure_mode` 接收编排层决策：普通 foreground ReAct 使用 `continue_to_model`，把失败结果作为
observation 交回主 LLM，由模型修改参数、选择其他工具、追问或诚实解释失败；传统计划、durable task 和
其他未显式选择恢复模式的入口默认仍使用 `stop_run`。取消始终走独立 cancellation 终态，不能作为普通
工具失败恢复。该状态机不得按 Tool name 分支。

foreground ReAct 的重复保护使用 `tool_name + canonical tool_input` 的摘要签名。相同工具使用不同参数
属于可修正调用，可以继续执行；完全相同且已经失败的调用在执行前被阻止，并增加结构化 rejected
observation，随后用不暴露工具的 answer-only LLM 轮次形成最终回答。这样既允许修正输入，也不会用
相同参数反复请求 Provider。成功、失败和拒绝事件继续进入 trace；LLM 已处理工具失败并返回最终文本时，
run 终态为 `completed`，响应通过 `degraded` 和 `handled_tool_failures` 保留诊断事实。
`write` / `dangerous` 工具失败后可能存在副作用结果不确定性，因此 foreground ReAct 不自动修正或
重试；下一轮直接进入 answer-only，由 LLM 解释失败或要求用户重新发起。

幂等语义不再由通用 ToolSpec 治理。具体 provider 若需要 idempotency key，应在领域 schema/adapter 或
durable task 协议中处理；通用 executor 不维护进程内重复调用 ledger。

durable task 的通知同样不属于 ToolExecutor 的渠道副作用。一个 quantum 只能在 terminal checkpoint
中提交 `TaskNotificationRequest`；`DurableTaskService` 使用任务的可信 `user_id` / `agent_id`
绑定 owner 和 `destination_ref`，再写入共享 `NotificationEnvelope` outbox。通知正文和目的地址不进入
TaskEvent，事件只记录 delivery id、channel、状态、尝试次数和安全 reason code。实际发送由
`NotificationDeliveryWorker` 通过 lease、重试、过期和 dead-letter 状态机完成；订阅取消、worker
重启或相同 idempotency key 重放都不能制造第二次发送。默认测试只使用 mock transport。

`lodging_search` 属于内置 `lodging` Tool Plugin，只提供结构化酒店报价读取，明确不提供预订、占房或
付款。mock mode 注册确定性本地 adapter；real mode 在没有显式稳定 Provider adapter 时不注册，
不得回退到 mock。`hotel_price_watch_v1` durable workflow 可以重复调用它，但每次调用仍执行同一
validator/executor/registry 治理，并受 task attempt、quantum、deadline、cancel 和 notification
idempotency 约束。`hotel_price_watch_create` 只在 durable tasks 已启用且请求显式进入 durable/计划
模式时暴露；普通 foreground 请求在 ActionValidator 边界拒绝它。

工具不再各自声明 trace 脱敏策略。完整内容开关是本地运行级事实：默认关闭；开启后仍排除
`raw_provider_payload`、`provider_raw_response` 和内联大块数据，并继续执行 secret、base64、绝对路径
和长度清理。real 模式不应开启该变量。

## 6. 写工具执行语义

系统不维护第二次用户确认状态。Tool 被本轮 `RunToolCatalog` 暴露并通过 `ActionValidator` 后，
`ToolExecutor` 直接调用 `tool.run()`。`category=write|dangerous` 仍用于暴露策略、禁用自动重试、
Trace 和副作用分析；具体 Provider 的幂等键继续由领域 schema/adapter 或 durable task 注入。

## 7. MCP、本地工具和 workflow

MCP 定义先经过 server allowlist，再转换成 namespace tool name 和简单 ToolSpec：read-only MCP 工具是
`category=read`；其他 MCP 工具是 `category=write`。注册后的 MCP proxy 走相同
`ActionValidator -> ToolExecutor -> ToolRegistry` 链路。

### 7.1 weather / calendar / email 真实 MCP 配置

仓库提供 `deploy/mcp_servers.example.json` 作为无凭据模板。部署时复制到默认忽略的
`.local/mcp_servers.json`，并只在本地配置实际 MCP Server 命令、参数和认证环境：

```bash
cp deploy/mcp_servers.example.json .local/mcp_servers.json
export MULTIMODAL_AGENT_PROVIDER_MODE=real
export MULTIMODAL_AGENT_MCP_ENABLED=1
export MULTIMODAL_AGENT_MCP_CONFIG_PATH=.local/mcp_servers.json
```

当前模板固定使用 `mcp_weather_server==0.6.1` 和 `workspace-mcp==1.22.0`，通过
`hello_agent` 环境中的 `uvx` 隔离运行，不把它们加入项目运行依赖。Calendar 命令显式附加
`PySocks`，使上游 `httplib2` 能在代理网络中访问 Google API。首次启动仍会由 `uvx` 下载对应环境；
本机必须先显式安装 `uv`。模板中的 `calendar_user_email` 必须替换为完成 Google OAuth 的账号，
`GOOGLE_OAUTH_CLIENT_ID` 和 `GOOGLE_OAUTH_CLIENT_SECRET` 只从本地 shell 或未跟踪 `.env` 注入，不能写入
MCP 配置模板或提交。当前 stdio 单机配置使用 `http://localhost:8000/oauth2callback`，因此本地 OAuth
需要 `OAUTHLIB_INSECURE_TRANSPORT=1`；该开关不得用于公开或非 loopback 部署，公开部署必须改用 HTTPS
并在 Google Cloud 中登记完全一致的 redirect URI。stdio MCP 子进程继承宿主的标准
`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 `NO_PROXY`（含小写形式），同名 server `env` 显式配置
优先；同时存在 HTTP/HTTPS proxy 与 `ALL_PROXY` 时只传递前者，避免上游客户端误选不兼容的 SOCKS
scheme。其他环境变量仍由 MCP SDK 的安全白名单或 server `env` 控制。真实模式还要求主 Chat
Provider 完整配置；MCP 配置不会绕过这个全局边界。
weather 和 contacts 只有存在对应 `personal_assistant_tools` mapping 时才注册，映射的远端工具还
必须同时位于 `allowed_tools` 与 `read_only_tools`。本地 calendar 不要求 MCP mapping；
`calendar_create` 仍按 write Tool 暴露规则治理。
`email_search` 和 `email_read` 只有存在独立 `email_tools` mapping 时才注册；两个远端 Gmail 工具必须
同时位于 `allowed_tools` 与 `read_only_tools`。推荐使用独立 `google_gmail_readonly` MCP server，并
以 `workspace-mcp --tools gmail --read-only` 限制 OAuth scope 和远端工具集合。

两个已支持 profile 都是显式配置，不根据工具名猜测 Provider：

- `mcp_weather_server_v1` 把稳定 `weather` 输入转换为 `get_weather_byDateTimeRange`，再把小时数据聚合为
  每日 forecast；该上游目前要求英文城市名。这个要求由 adapter 的结构化输入能力声明进入实例级
  Weather ToolSpec，提示 LLM 在构造工具参数时把本地化地点名翻译为规范英文名，同时保持最终回答使用
  用户语言；不得在 Gateway、catalog 或 adapter 中维护城市名映射表。上游无法解析地点时归一化为
  `provider_unsupported_input` observation，只有参数发生变化后才允许再次调用。
- `workspace_mcp_v1` 把稳定 `calendar_search` / `calendar_create` 分别转换为 `get_events` /
  `manage_event`，并注入本地 `calendar_user_email`；创建动作固定为 `action=create`。

`calendar_create` 只有在入口 profile、host 配置或本轮 `enabled_tools` 明确暴露时才可调用；暴露并
通过校验后直接执行。真实天气与日历能力只能在 operator 显式启用真实工具的评测中执行。该命令让真实 LLM 经过 Runtime
和工具治理链路自主调用外部 Provider，失败时明确报告：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_tool_evals.py \
  --allow-real-tools \
  --case-id weather_beijing_today
```

本地 `@tool` decorator 直接声明 `category` 和 `requires_media`，不再要求 rich policy 或
per-tool timeout/retry metadata。CLI validate 检查能否生成
合法 ToolSpec；simulate 仍通过 validator/executor 执行。

Skill 只能调用已注册且 permission 匹配的工具。read 工具允许按 Skill retry 配置重试；
非 read step 的幂等与重试语义由 Skill/领域实现显式承担，不由中心 Tool manifest 声明。

## 8. 代码导航

- `tools/models.py`：`ToolSpec`、`RunToolCatalog`、`ToolResult`、`ToolCallRecord`；
- `tools/ids.py`：仅供既有跨层协议共享的稳定 Tool/capability 字符串；
- `runtime/legacy_tool_mapping.py`：旧 planner/intent action 与 capability 兼容映射；
- `tools/plugins/contracts.py`、`assembly.py`、`defaults.py`：Plugin 协议、原子装配和可信内置清单；
- `tools/plugins/registry_factory.py`：默认 Plugin、MCP proxy 和 realtime observer 的 Registry
  composition root；
- `tools/plugins/builtin/<assembly_boundary>/`：按共享 Provider、配置、依赖和生命周期组织的内置 Plugin
  及其 Tool、私有 adapter/backend 实现；
- `tools/base.py`：公共 Tool 协议；
- `tools/registry.py`：工具注册、查找和 Pydantic schema 提取；
- `context/tool_catalog.py`：结构化目录装配；
- `context/tool_exposure.py`：category/profile/media 暴露规则；
- `tools/spec_adapters.py`：OpenAI/MCP schema 转换；
- `runtime/action_validator.py`：run catalog、Pydantic、media、durable 校验；
- `runtime/tool_executor.py`：身份绑定、调用和提交；
- `tests/contract/tools/test_tool_governance.py`：工具治理稳定契约。

## 9. 不变量

- 所有模型驱动工具调用必须经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`；
- 暴露给模型的工具一定可进入执行链路，未暴露工具一定被 validator 拒绝；
- category/profile/media 只基于结构化事实，不从自然语言推断；
- Pydantic schema 是工具参数形状的权威；
- 主模型工具调用链对同一输入只构造一次 Pydantic model；
- Memory、MCP、durable task、Skill、CLI 和 Gateway 不绕过统一工具边界；
- 默认测试与 eval 保持 mock/local/offline，真实 provider 必须显式启用。
- 任何普通工具执行失败都先是 ToolResult/observation；只有编排恢复策略、取消或 Runtime 自身失败可以决定
  Agent run 的 terminal status，Gateway 不按工具名或 Provider 错误码改写终态。
