# 工具调用架构

本文是 `assistant_agent` 工具注册、暴露、调用和执行边界的当前权威说明。源码和测试优先于本文。

## 1. 文档边界

本文只记录跨工具稳定成立的架构：

- 公共数据契约及输入所有权；
- Tool 注册、单轮暴露、Provider schema 转换和执行链路；
- Plugin、MCP、Skill、durable task 与 Provider-native 能力的边界；
- 失败、重试、取消、审计和安全不变量；
- 权威源码与测试入口。

本文不记录单个 Tool、Provider 或部署实例的字段、参数、环境变量、命令、业务流程、返回样例和兼容
历史。具体能力以其 Tool schema、Plugin、adapter、配置、专项文档和测试为准。开发计划、历史 spec
和 eval case 也不作为本文的当前架构事实。

## 2. 总体模型

工具系统把“已安装能力”“本轮可见能力”和“一次实际执行”分成三个边界：

```text
Plugin / MCP source
  -> ToolRegistry（启动期注册并 seal）
  -> ToolSpec（Provider-neutral 契约）
  -> RunToolCatalog（单轮可见且可执行的集合）
  -> Provider 返回的 native function/tool call
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
  -> tool.run()
  -> ToolResult / ToolObservation
  -> assistant loop
```

核心不变量是：

> 本轮暴露给模型的工具，就是本轮允许模型调用的工具。

`RunToolCatalog.available_tool_names` 是唯一的 run-scoped 可用集合，不再维护另一份 executable
allowlist。Registry inventory 与单轮 catalog 含义不同，不能互相替代。

公共运行时不根据请求关键词、正则或手写业务规则选择工具。候选集合只能由 Registry、入口显式限制、
媒体状态、可信 durable step 等结构化事实确定；是否调用、调用哪个候选工具以及如何填写模型拥有的
参数由 LLM 决定。

`visual_memory_search` 遵守同一边界：它是唯一新增的历史视觉 Tool，category 为 `read`，不要求本轮
附带媒体。Runtime 只依据同 user/session `SessionVisualSemanticStore.has_searchable_history()` 生成可信 exposure fact，并覆盖调用方
同名 metadata；模型不能提交 session、as-of sequence、evidence path 或 embedding。执行仍完整经过
Validator、Executor 和 Registry。查询只比较 query text 与已索引 VLM 文本的 embedding，不再次调用
VLM。Attention、alignment、keyframe 与 embedding Provider 都是内部组件，
不注册为 Tool。专项事实见 `multimodal-embedding-architecture.md`。

## 3. 公共契约

### 3.1 Tool 与 ToolSpec

Tool 实现统一声明：

```text
name
description
input_schema / output_schema
category: read | generate | write | dangerous
requires_media
media_scope: any | attached | live
llm_hidden_input_fields
runtime_input_bindings
run(input, context) -> ToolResult
```

`ToolSpec` 是 Registry 从 Tool 构造的 Provider-neutral 模型可见契约：

```text
name / description
input_schema
category
requires_media
media_scope
```

Registry 的 `get_spec()` 与 `list_specs()` 使用同一个 builder，Provider adapter、catalog、validator
和 executor 因而读取同一份静态声明。系统不维护按工具名复制 schema、category 或调用说明的中心
manifest；`tools/ids.py` 只保存已经成为跨层协议的稳定字符串。

`category` 表达副作用、自动重试、失败恢复和审计语义，不负责推断用户意图，也不控制默认暴露。
未声明分类的 Tool 使用 `dangerous` 作为保守默认值。

### 3.1.1 异步生成任务与入口投递

异步 Provider-backed Tool 的“提交成功”“任务完成”和“入口投递成功”是三个不同状态。Tool 可以在
提交阶段返回中性 `job_id/status`，外部 callback 先更新由 runtime/tool 侧拥有的任务结果，再由入口
capability 决定是否做 WebSocket、通知等入口特定投影。callback 不应把“存在某种入口连接”当作任务
完成的前置条件，也不应重新进入 LLM 规划。

`image_to_3d` 是当前具体实现：Tool 创建 owner-bound job，3D callback 保存 artifact；只有可信
Agent-Service capability 允许媒体投递，HTTP client 通过 owner-bound API 查询。具体 wire 字段、
兼容路径和当前进程内存限制见 `media-agent-service-websocket.md`。

### 3.2 输入所有权

Tool 的 Pydantic `input_schema` 是完整执行输入和校验的事实源。`ToolSpec.input_schema` 是从该模型
投影出的 LLM 可见视图。字段只有三种所有权：

- **模型输入**：出现在 ToolSpec schema 中，由 LLM 提交；无默认值的字段按 Pydantic 规则进入
  `required`；
- **工具默认输入**：具有 Pydantic 默认值；默认仍可由 LLM 覆盖，只有列入
  `llm_hidden_input_fields` 后才从模型 schema 隐藏并禁止覆盖；
- **运行时输入**：通过 `runtime_input_bindings` 声明，由可信运行态按次注入，不发送给模型。

注册时必须验证绑定字段和隐藏字段真实存在、互不重叠、没有重复绑定，并且隐藏字段具有默认值。
运行时输入只能来自声明过的结构化来源；内部 worker 或 observer 提供的显式 `runtime_input` 也只能
覆盖已声明为 runtime-owned 的字段。

模型提交未知字段、runtime-owned 字段或隐藏默认字段时必须被确定性拒绝，不能依赖 Pydantic
静默忽略。正常模型调用路径由 `ActionValidator` 完成绑定和 Pydantic 构造，
`ToolExecutor` 复用 `validated_input`；只有执行边界新增可信 runtime 输入时才重新校验。

### 3.3 RunToolCatalog

`RunToolCatalog` 是一个 assistant turn 的工具目录快照：

```text
schema_version: run_tool_catalog_v1
available_tool_names
selection_reasons
excluded_reasons
```

`available_tool_names` 同时用于：

- 选择发送给 Provider 的 ToolSpec；
- 记录本轮 context 与 trace 中的工具集合；
- 拒绝模型猜测出的未暴露工具名。

`selection_reasons` 和 `excluded_reasons` 只解释结构化选择结果，不授予额外权限。已注册工具的完整
清单仍由 Registry 提供。

### 3.4 ToolResult 与模型观察

Tool 必须返回结构化 `ToolResult`。其中：

- `success` 表示执行契约是否成功完成；
- `data` 保存完整的 runtime、API 或 trace 结果；
- `model_observation` 是 Tool 拥有的模型视图，进入 prompt 前仍由通用 observation 边界清理；
- `trace_summary`、`audit_payload` 和引用字段服务于各自边界；
- `error` 保存经过清理、可解释的失败信息。

完整结果、模型观察、Trace 摘要和最终用户交付是不同投影，不能把某一投影当作其他边界的规范事实
源。未提供 `model_observation` 的旧 Tool 允许从 `data` 构造兼容观察；新 Tool 应显式提供最小模型
视图。assistant loop 把模型可见 observation 交回 LLM，使其能够继续调用其他工具、修正参数、追问
或基于已有证据作答。

## 4. 注册与装配

### 4.1 ToolRegistry

`ToolRegistry` 负责注册、查找、生成 ToolSpec 和调用 Tool。默认 runtime 在启动期完成装配后 seal
Registry，并基于注册记录与 ToolSpec 生成稳定 generation。seal 后不能继续注册；配置变化通过重启
生效。

批量注册先验证整批贡献，再原子提交。重复 Tool name、无效输入契约、Plugin 协议错误或装配失败
必须 fail closed，不能留下半装配 Registry。

### 4.2 Plugin

Plugin 是启动期代码装配和所有权边界，不是 Tool 的执行协议，也不授予单轮执行权限。Plugin 按共享
Provider、配置、依赖和生命周期组织，通过 `build_tools(context)` 构造 Tool；composition root 统一
完成发现、验证、注册和 seal。

内置 Plugin 使用显式可信清单，不扫描目录。外部进程内 Plugin 只从部署配置显式导入；导入 Python
module 等同于执行受信任代码，不是不可信插件沙箱。不可信或跨进程能力应放在 MCP 或独立服务边界。

增加普通 Tool 时只修改其所属 Plugin，不应要求修改 Registry、Validator、Executor、assistant loop
或中心 Tool name 表。只有新的宿主级共享依赖才扩展 Plugin context 和 composition root。

### 4.3 Website guidance

`website_guidance` 是默认关闭的内置 Plugin。显式启用
`MULTIMODAL_AGENT_WEBSITE_GUIDANCE_ENABLED=true` 后，才尝试注册 `web_page_inspect` 与
`web_page_explore`。前者归类为 `read`；后者会触发受限页面事件，归类为 `dangerous`，不进入只读
自动重试。两者都是本地显式 Tool，必须经过
`ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。Qwen Provider-native 搜索只帮助模型发现
候选 URL；候选仍须通过 `web_page_inspect` 的公开 URL 与 SSRF 策略验证，不能视为已打开页面或可信
页面证据。

`web_page_inspect` 为公开 HTTP(S) 页面生成有界快照和不透明 browser session。结果携带
`requested_url`、已验证的 `final_url`、带时区的 `checked_at` 和 `is_complete`。只有未重定向且证据
完整的页面可返回 `success`；同 origin 重定向为 `partial`，缺少最终证据时只能返回 `blocked` 或
`failed`。`web_page_explore` 必须使用 inspect 发放并绑定 `run_id + session_id` 的 session；只有
`click` 动作需要上一快照中的 `eN` 引用。

页面 title、正文、元素名称和链接均是 `untrusted_external_content`，只能作为分析证据。首版不支持
脚本、selector、任意 URL 跳转、表单填写、登录、下载、弹窗或提交动作。链接动作只按已验证的
`href` 导航，不执行 anchor `onclick`。可展开按钮必须具有唯一稳定 DOM `id`，并在重放、点击前后
绑定和复核同一个 `ElementHandle`；展开期间启用 network-silent guard，即使同 origin `GET` 也会在
离开浏览器前被拒绝。

真实 backend 使用 Playwright Chromium 的 headless 短生命周期上下文，禁用 service worker，并拒绝
非 `GET`/`HEAD`、跨 origin 资源和 WebSocket。每次导航、重放和动作后的最终 URL 都重新执行公网解析
与同 origin 校验；策略拒绝非 HTTP(S)、认证信息、localhost、非标准端口和非公网地址。document
`Response` 还必须通过状态码、headers、redirect chain、HTML/XHTML MIME、attachment、认证挑战及
Response/page URL 一致性校验，否则 fail closed。

session store 只保留 owner、可安全动作和有界快照元数据，不保存 cookie、正文或登录状态，并施加
TTL、每 run、全局和单快照容量限制。浏览器操作在启动、导航、重放、等待、快照和提交元数据边界
协作检查 cancellation；Runtime 在 completed、failed、cancelled 终态通过 Registry 的 best-effort
lifecycle hook 清理该 run 的 session 元数据。

mock mode 只对声明的 fixture URL 返回确定性证据，其他 URL 返回 `mock_url_unverified`；mock session
也只在成功 inspect 后发放并绑定 owner。real mode 仅在功能启用、Playwright/Chromium ready 且 backend
构造成功时注册真实 Tool，缺依赖、readiness 或构造失败都 fail closed，不回退到 mock。导航 timeout
使用 `WEBSITE_GUIDANCE_NAVIGATION_TIMEOUT_SECONDS`，只接受大于 0 且不超过 30 秒的值。

### 4.4 MCP

MCP 是外部 Tool source。远端定义先经过 server 配置、allowlist、read/write 分类和 namespacing，
再作为 proxy Tool 参与同一批注册。注册完成后，MCP Tool 与进程内 Tool 使用相同的 ToolSpec、
RunToolCatalog、Validator、Executor、结果和审计边界。

MCP server、认证、远端方法映射和部署命令属于配置或对应集成文档，不进入本文。

## 5. 单轮暴露与 Provider 转换

注册成功的 Tool 默认是候选能力；入口 `allowed_tools`、媒体要求和可信 durable ready step 可以继续
收窄当前 turn 的集合。Tool `category`、Plugin 归属和 Skill 激活不自动赋予或扩大权限。

媒体约束由 `requires_media` 与 `media_scope` 基于结构化请求事实判断。模型看不到不满足当前媒体
条件的 Tool，Validator 仍会在执行前重复检查，防止状态漂移或伪造调用。

ToolSpec 转换成 OpenAI-compatible 或 MCP schema 时，应保留名称、简短描述、Pydantic
`properties`、`required` 和可表达的约束。为减少 prompt 体积，可以在渲染时移除标题或截短描述，
但不能改变规范 ToolSpec 或削弱执行期校验。

系统不维护基于自然语言意图的 tool preselection、独立 toolset 或 Tool Search。工具规模由部署时
安装的 Plugin、MCP allowlist 和入口显式限制控制。

## 6. 校验与执行

### 6.1 ActionValidator

模型提出 tool call 后，`ActionValidator` 依次校验：

1. Tool 已注册；
2. 当前 task execution mode 和可信 durable binding 允许该调用；
3. Tool 位于当前 `RunToolCatalog`；
4. 当前媒体类型和作用域满足 ToolSpec；
5. 模型没有提交未知字段、runtime-owned 字段或隐藏默认字段；
6. runtime binding 后的完整输入通过 Pydantic schema；
7. Tool 自有 `validate_call()` 领域规则通过。

Validator 返回稳定 code、可解释 message、prompt-safe metadata 和仅供进程内复用的
`validated_input`。校验拒绝不会进入 Executor。

### 6.2 ToolExecutor

`ToolExecutor` 是统一执行和生命周期提交边界，负责：

- 检查协作式取消；
- 绑定每次调用的身份、请求状态和显式可信 runtime 输入；
- 创建 Tool call record，发布 `tool.started`；
- 按 `category` 和执行策略处理有限自动重试；
- 通过 Registry 调用 `tool.run()`；
- 记录真实墙钟延迟、结果、恢复决策与 terminal event；
- 把成功或失败提交回 `AgentState`。

一次 Executor 自动重试保持相同 tool call 和输入；模型看到 observation 后修改参数再次调用属于新的
assistant action。通用 Executor 不维护跨进程幂等 ledger；外部写入需要的幂等键由领域 adapter、
协议或 durable task 提供。

取消可能发生在执行前、重试等待期间或 Tool 返回之后。读操作成功后发现取消时不能继续发布为有效
结果；非只读操作则必须保留已经发生的副作用事实，并以结构化取消状态结束，不能伪装为未执行。

### 6.3 失败与编排

普通 Tool 失败首先是结构化 `ToolResult` 和 observation，不自动等于整个 Agent run 失败。assistant
loop 或上层 workflow 根据恢复策略决定修改输入、改用其他工具、追问、基于已有证据回答或终止。
Gateway 不按 Tool name 或 Provider 错误码改写运行终态。

`category=read` 才允许通用自动重试；其他 category 默认不自动重试。写入和危险操作在进入 catalog
并通过 Validator 后由 Executor 直接执行，公共运行时不维护第二次用户确认状态。需要确认、授权或
幂等的能力必须把这些要求放进入口授权、Tool schema、领域 adapter 或 durable protocol。

## 7. 相邻系统边界

- **Provider-native 能力**：发生在一次 `llm.chat` 内部的 Provider 生成能力不投影为本地 Tool，
  不产生本地 ToolResult 或 Tool lifecycle；Provider 返回的自定义 function call 仍进入统一链路。
- **Skill**：只描述如何组合已治理 Tool，不注册业务实现、不扩大 catalog，也不绕过校验和执行。
  Skill 正文与 reference 的加载、上下文权威和渐进披露见
  `docs/context_engineering_status.md`。
- **Durable task**：只通过可信 task mode、ready step、binding 和幂等输入收窄或约束执行；
  worker 调用仍走统一工具链。任务恢复、lease、notification 和 checkpoint 属于 durable/runtime
  权威，不由通用 Executor 代管。
- **Memory**：记忆读写遵循 `MemoryManager` 与 memory policy；默认长期记忆不是主模型可调用 Tool。
- **Gateway、CLI、API、demo、eval**：都是入口或观察形态，不能直接调用 Tool 实现来复制 Agent
  逻辑。
- **内部 Tool**：后台 observer 或 worker 可以使用独立 Registry/catalog，但仍必须经过 Validator
  和 Executor；内部身份不等于绕过治理。

## 8. Provider 模式与安全

Provider 运行只分 `mock` 和 `real`：

- mock 模式强制主 LLM 与 Provider-backed Tool 使用 mock/local/offline 实现；
- real 模式要求主 LLM 完整配置，只注册 readiness 完整的真实 Provider Tool；
- 缺少真实配置时 fail closed，禁止静默回退到 mock；
- 检测到 key 不会自动开启真实调用。

Tool description、输入、结果、error、event 和 trace 都必须经过各自的内容与脱敏策略。原始 Provider
payload、凭据、绝对路径和大块内联数据不能因 ToolResult 或调试开关越过安全边界。详细观测契约见
`docs/observability-harness.md`。

## 9. 代码导航

- `tools/base.py`：`Tool`、`ToolBase`、`ToolContext` 和 Tool 自有校验错误；
- `tools/models.py`：`ToolSpec`、`RunToolCatalog`、`ToolResult`、`ToolCallRecord`；
- `tools/input_binding.py`：输入所有权声明、启动校验和 runtime binding；
- `tools/registry.py`：注册、seal、查找和 ToolSpec 投影；
- `tools/plugins/contracts.py`：Plugin descriptor、context、registration 与 assembly report；
- `tools/plugins/assembly.py`：Plugin 发现、全量校验和原子装配；
- `tools/plugins/defaults.py`：可信内置 Plugin 清单；
- `tools/plugins/registry_factory.py`：默认、MCP 和内部 Registry composition root；
- `tools/plugins/builtin/`：具体 Tool、Plugin 与私有 adapter/backend；
- `context/tool_catalog.py`：结构化单轮 catalog 装配；
- `context/tool_exposure.py`：媒体等结构化暴露条件；
- `tools/spec_adapters.py`：Provider schema 转换；
- `runtime/action_validator.py`：run catalog、输入、媒体与 durable 校验；
- `runtime/tool_executor.py`：调用、重试、取消、状态提交和生命周期事件；
- `tools/observation.py`：ToolResult 到模型观察的通用投影；
- `tests/core/contract/test_tool_contract.py`：`TOOL-001` 核心治理契约。

## 10. 不变量

- 所有模型驱动的本地显式 Tool call 必须经过
  `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`；
- 暴露给模型的 Tool 一定可进入执行链路，未暴露 Tool 一定被 Validator 拒绝；
- catalog 只读取结构化运行事实，不从自然语言推断用户意图；
- Pydantic schema 是完整输入形状和校验的权威，ToolSpec 是其模型可见投影；
- 模型不能提交或覆盖 runtime-owned 输入与隐藏默认输入；
- Registry、catalog、Provider schema、Validator 和 Executor 都从 Tool/ToolSpec 派生当前契约；
  兼容投影不能成为注册或暴露的事实源；
- Plugin、MCP、Skill、durable task、Memory、Gateway 和内部 worker 不绕过统一工具边界；
- 普通 Tool 失败先形成 ToolResult/observation，再由编排层决定 Agent run 的后续状态；
- 默认测试与 eval 保持 mock/local/offline，真实 Provider 必须显式启用。
