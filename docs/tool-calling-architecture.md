# 工具调用架构

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Tool 注册、暴露、调用、执行与 durable workflow 治理的当前权威 |
| Owns | Tool/ToolSpec、catalog、Plugin、MCP、Validator、Executor、Workflow Tool 与副作用边界 |
| Does not own | 用户意图关键词路由、Gateway 生命周期、Memory Plugin 生命周期、Provider vendor 私有协议 |
| 源码与 schema 入口 | `src/assistant_agent/tools/`、`src/assistant_agent/workflows/`、`src/assistant_agent/mcp/` |
| 验证入口 | `docs/authority.toml` 中 `tool-calling.verification` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md) |

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

通用长阶段任务遵守同一规则。启用
`MULTIMODAL_AGENT_DURABLE_WORKFLOWS_ENABLED=true`、绑定 `WorkflowService` 且至少存在一个
已注册 `WorkflowDefinition` 时，builtin Plugin 才注册 `workflow_submit`。是否调用由现有
Provider-native ReAct LLM 自主决定；入口不增加关键词路由、正则分类或独立 assistant-decision LLM。
该 Tool 只原子创建持久 Workflow 并返回 handle，不在前台 run 内执行完整流程。普通任务仍走当前
assistant loop。

显式产品模式是结构化入口事实，不是文本意图路由。当前唯一新增模式
`assistant_mode=deep_research` 会把前台 RunToolCatalog 收窄为 `workflow_submit`，并在首次 Provider
决策中要求选择该 Tool；用户文本不会隐式开启该模式。模式提交成功后仍由既有 durable Workflow
执行，普通 `assistant_mode=standard` 不改变现有工具目录和 assistant loop。

`visual_memory_search` 遵守同一边界：它是唯一新增的历史视觉 Tool，category 为 `read`，不要求本轮
附带媒体。Runtime 只依据同 user/session `SessionVisualSemanticStore.has_searchable_history()` 生成可信 exposure fact，并覆盖调用方
同名 metadata；模型不能提交 session、as-of sequence、evidence path 或 embedding。执行仍完整经过
Validator、Executor 和 Registry。查询只比较 query text 与已索引 VLM 文本的 embedding，不再次调用
VLM。Attention、alignment、keyframe 与 embedding Provider 都是内部组件，
不注册为 Tool。专项事实见 `multimodal-embedding-architecture.md`。

`visual_reminder_manage` 是连接级视觉提醒的有状态 `write` Tool，支持 `create/list/cancel`。它只在
可信 Agent-Service VIDEO entry、结构化 call type 和活动 owner/session manager 同时成立时暴露；
session 由 runtime identity 注入，模型不能提交 manager、owner、embedding 或阈值。`create` 只编码
一次视觉条件 target，`list/cancel` 不调用 embedding Provider。关键帧匹配、一次性状态转换和主动
message 发布由 Runtime 持有的 registry 负责；Agent-Service 只注入 `ProactiveMessageSink` 并投影为
`chatResponse`。创建、查看和取消仍完整经过 Tool 治理链。

需要 VLM 的 `media_inspect`、显式视频理解和内部 `realtime_video_observe` 仍由视觉 Tool 拥有参数绑定、
授权、业务语义与 `ToolResult`；具体模型调用统一经过 Provider-neutral `VisionUnderstandingClient` 和
vision/video adapter。VLM 是 Tool 的内部 Provider 能力，不注册成主 LLM 可见的通用 Tool，具体 Tool
也不得直接依赖某一家 Provider SDK。`visual_memory_search`、`visual_reminder_manage` 等不需要视觉
推理的 Tool 不得为了统一形式额外调用 VLM。

上述视觉理解 Tool 声明 `trace_content_policy=metadata_only`。这只收窄 canonical Tool event、当前 turn
的 trace conversation overlay 和 Langfuse Tool observation，不改变交给主 LLM 的结构化 Tool
observation；因此 Agent 仍能依据视觉结果回答，但对应 Tool span 不包含媒体引用、视觉正文、本地路径
或 Provider 失败原文。

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
repeat_policy: once_per_run | distinct_inputs
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
repeat_policy
```

Registry 的 `get_spec()` 与 `list_specs()` 使用同一个 builder，Provider adapter、catalog、validator
和 executor 因而读取同一份静态声明。系统不维护按工具名复制 schema、category 或调用说明的中心
manifest；`tools/ids.py` 只保存已经成为跨层协议的稳定字符串。

`category` 表达副作用、自动重试、失败恢复和审计语义，不负责推断用户意图，也不控制默认暴露。
未声明分类的 Tool 使用 `dangerous` 作为保守默认值。

`repeat_policy` 表达一次 run 内的 Tool 级重复调用边界。`once_per_run` 在已有一次成功执行后拒绝
同工具的后续调用；`distinct_inputs` 允许不同规范化输入继续调用，但参数完全相同且已有成功结果时
仍复用已有结果。失败调用不消耗成功额度，完全相同的失败输入仍由失败去重 guard 处理。所有策略
都受全局 `max_tool_iterations` 限制；该字段是 Runtime 治理事实，不进入 Provider Tool schema。

每个内置 concrete Tool 必须显式声明策略。当前 `task_plan_submit`、`image_generation` 和
`image_to_3d` 使用 `once_per_run`，其余内置 Tool 使用 `distinct_inputs`。`ToolSpec` 与 `ToolBase` 的
默认 `once_per_run` 只为旧式或外部 Tool 提供保守兼容回退，不能替代内置 Tool 的显式分类。
Runtime 只读取 Registry 投影后的 `ToolSpec.repeat_policy`，不按工具名维护终止工具清单或第二套
重复执行配置。

### 3.1.1 异步生成任务与入口投递

异步 Provider-backed Tool 的“提交成功”“任务完成”和“入口投递成功”是三个不同状态。Tool 可以在
提交阶段返回中性 `job_id/status`，外部 callback 先更新由 runtime/tool 侧拥有的任务结果，再向
Gateway 的媒体无关 Artifact Delivery Hub 发布中性完成事件。是否订阅该事件、投影为 WebSocket、
通知或 UI 更新由入口 adapter 决定。callback 不应把“存在某种入口连接”当作任务完成的前置条件，
也不应重新进入 LLM 规划。

`image_to_3d` 是当前具体实现：Tool 创建 owner-bound job，3D callback 保存 artifact 并发布
`artifact.completed`。Tool、adapter 和 job 均不读取或保存入口 capability、sink、媒体连接或
Media-Agent 类型。Agent-Service adapter 以 session subscriber 身份完成媒体投影；HTTP client 通过
owner-bound API 查询。具体 wire 字段、兼容路径和当前进程内存限制见
`media-agent-service-websocket.md`。

连接级视觉提醒不是 durable notification。`visual_reminder_manage(create)` 成功只表示提醒已写入当前
活动连接 manager；后续已选关键帧命中后，Runtime registry 立即构造 `connection_ephemeral`
`ProactiveMessage` 并交给 Runtime-owned 后台 delivery task，不再次调用主 LLM，也不阻塞后续 VLM
队列。Agent-Service sink 等待已有普通 chat task 结束后通过当前 WebSocket 串行发送；只有
`server_transport` sent 才转为 triggered，失败或有界超时且连接仍活动时恢复 pending。连接关闭时
Runtime registry 取消 delivery task 并直接清空，
不写 notification outbox、不跨连接重放，也不使用 `chatResponseAck` 持久化确认。

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

Skill 认领的业务 Tool 默认不进入 `RunToolCatalog`。未激活的 `activation=model` Skill 只在
`load_skill` 本轮结构化可用且至少一个受治理 Tool 通过入口、媒体和 policy 资格检查时进入可发现
索引；模型成功调用 `load_skill` 后，Runtime 从仓库内 `skill.toml` 构造 `CapabilityGrant`，下一次
模型调用才把该 Skill 已声明且仍满足本轮资格的 Tool 加入目录。调用方 metadata（包括历史
`enabled_skills` 字段）不能生成 grant。可信 Workflow work-item 与 durable ready-tool allowlist 仍按其
既有白名单直接收窄目录，不依赖模型加载 Skill，也不把前台 session Skill 正文投影进 worker prompt。
成功的 `load_skill` 结果同时返回 `granted_tools` 与 `unavailable_tools`：前者是 manifest 声明、启动期
Registry 和本轮结构化资格的实际交集，后者保留其余声明项，使 MCP 未注册、入口限制或媒体条件不足
等降级不会被误报为已授予能力。

`CapabilityGrant` 只保存共有身份和 Tool 集合，具体实例分为三类：模型加载程序性正文产生
`SkillGrant(skill_id)`；entry/media/env 等结构化资格事实产生 `ContextToolsetGrant(toolset_id)`；未来可信
Tool Search 产生 `DeferredToolsetGrant(toolset_id)`。context Toolset 不可由 `load_skill` 调用，也不进入
active Skill 或正文投影，只扩展仍满足本轮结构化资格的 Tool。当前 grant 以
`user_id + agent_id + session_id` 隔离并持久到整个会话，不设 TTL 或主动清除；恢复时必须用当前
manifest 重建 Tool 列表，不能信任 session 中的旧 Tool 名称。旧 context 记录中的 `skill_id` 只在
反序列化边界迁移为 `toolset_id`。`DeferredToolsetGrant` 当前只建立类型与持久化契约；在没有可信搜索器
时继续 fail closed，不产生实际授权语义。

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

正常 Tool observation 使用独立的 prompt-safety 投影：清除 secret、raw payload、inline media、私有路径等
不安全内容，但不套用 Provider error detail 的固定列表上限，也不按统一元素数静默截断安全列表。
工具声明的结构化计数必须与该边界实际保留的列表一致；整体请求真正超过 context hard window 时应明确
阻断或由 Tool 专项预算策略压缩，并显式更新返回数和截断状态。失败详情仍使用 Provider error sanitizer
的有界策略。

## 4. 注册与装配

### 4.1 ToolRegistry

`ToolRegistry` 负责注册、查找、生成 ToolSpec 和调用 Tool。默认 runtime 在启动期完成装配后 seal
Registry，并基于注册记录与 ToolSpec 生成稳定 generation。seal 后不能继续注册；配置变化通过重启
生效。

批量注册先验证整批贡献，再原子提交。重复 Tool name、无效输入契约、Plugin 协议错误或装配失败
必须 fail closed，不能留下半装配 Registry。

上线前 Release Review 复用 Runtime 的 production Registry composition root，不自行追加模拟目录或
同名 Tool。Decision 场景由受信 composition root 向 `ToolExecutor` 注入无副作用 execution backend；
它只替换最终 invocation，Registry、ToolSpec、RunToolCatalog、Validator、状态生命周期和 trace 均保持
生产路径。Staging 场景使用默认 Registry backend 和隔离预发布资源。具体运行契约见
[`../evals/README.md`](../evals/README.md)。

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

MCP 的重复策略只根据 server 配置中的结构化只读声明映射：`read_only_tools` 中的工具使用
`distinct_inputs`，其余工具使用 `once_per_run`。直接生成 ToolSpec 与注册 proxy Tool 必须复用同一
映射；未提供可信只读声明时按写工具保守处理，不能根据远端工具名称或 description 猜测。

MCP server、认证、远端方法映射和部署命令属于配置或对应集成文档，不进入本文。

### 4.5 Durable Workflow Plugin

`durable_workflow` 是默认关闭的内置 Plugin。它从 `ToolPluginContext.workflow_service` 接收可信服务；
配置关闭、服务缺失或 definition catalog 为空时返回空工具列表，不能暴露一个无法推进的提交 Tool。
`workflow_submit` 是 `write/once_per_run/metadata_only` Tool，输入是通用
`WorkflowSubmission`：`workflow_type/objective/deliverables/constraints/inputs/initial_workstreams/
requested_budget/durability_reasons/seed_artifact_refs/idempotency_key`。Research 问题等业务字段只能放在
definition-owned `inputs` schema 中，不能污染通用契约。

Tool 从 `ToolExecutor` 注入的 `request_identity`、`run_id` 和同一 `WorkflowService` binding 构造
owner-bound submission；模型不能提交 owner、lease、revision、worker 或 Store。成功 observation 只
包含 `workflow_id/type/status/phase/status_url/events_url/event_cursor` 等安全 handle。重复
`user + agent + ingress_run + idempotency_key` 且 payload digest 相同返回既有 Workflow；不同 payload
返回结构化冲突。

## 5. 单轮暴露与 Provider 转换

注册成功的 Tool 默认是候选能力；入口 `allowed_tools`、媒体要求、CapabilityGrant 和可信 durable
ready step 共同决定当前 turn 的集合。grant 只能在结构化资格集合内打开 Skill 已声明的候选 Tool，
不能扩大 Registry、入口权限或用户授权。Tool `category` 和 Plugin 归属不自动赋予或扩大权限。

媒体约束由 `requires_media` 与 `media_scope` 基于结构化请求事实判断。模型看不到不满足当前媒体
条件的 Tool，Validator 仍会在执行前重复检查，防止状态漂移或伪造调用。

`visual_reminder_manage` 本身声明 `requires_media=[]`，使用户可以在 VIDEO handshake 后、第一帧到达
前创建提醒；它另有更严格的 runtime exposure fact。Runtime 会先删除调用方伪造的
`_trusted_visual_reminder_available`，只有可信 Agent-Service session config、`call_type=VIDEO` 和
registry 中活动 manager 同时成立时才重建该 fact。该规则只使用结构化连接状态，不读取请求文本。

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
- 默认通过 `RegistryExecutionBackend` 调用 Registry 中的 `tool.run()`；受信 Release Review Decision
  可在同一 Executor 内使用无副作用 `ScenarioExecutionBackend`；
- 记录真实墙钟延迟、结果、恢复决策与 terminal event；
- 把成功或失败提交回 `AgentState`。

一次 Executor 自动重试保持相同 tool call 和输入；模型看到 observation 后修改参数再次调用属于新的
assistant action。通用 Executor 不维护跨进程幂等 ledger；外部写入需要的幂等键由领域 adapter、
协议或 durable task 提供。

execution backend 只能由进程内受信 composition root 显式注入，不能由请求正文、metadata、模型输出或
Dataset 内容选择。无论使用哪种 backend，Executor 都必须先完成 Registry contract lookup、runtime
binding、生命周期事件和状态提交；生产与 Staging 默认 backend 始终实际调用 Registry Tool。

assistant loop 在 decision guard 与实际执行边界都读取 `ToolSpec.repeat_policy`，从而覆盖 sequential
与同一 Provider turn 的批量 tool calls。所有 category 的成功调用都会登记规范化调用签名；
`distinct_inputs` 的相同成功输入产生 `duplicate_complete_tool_call`，`once_per_run` 的第二次调用产生
`tool_repeat_limit_reached`。Tool 级成功限制不因失败调用生效；失败后的恢复继续服从
recoverable/non-recoverable 与相同失败输入去重规则。重复拒绝进入 finalize，不再继续消耗迭代。

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
- **Skill**：`SKILL.md` 只描述如何组合已治理 Tool；`skill.toml` 独立保存版本、激活模式和受治理
  Tool 等机器契约。Skill 不注册业务实现，CapabilityGrant 也只在既有结构化资格内动态扩展当前
  catalog，不能绕过校验和执行。Skill 正文与 reference 的加载、上下文权威和渐进披露见
  `docs/context_engineering_status.md`。
- **Durable task**：只通过可信 task mode、ready step、binding 和幂等输入收窄或约束执行；
  worker 调用仍走统一工具链。任务恢复、lease、notification 和 checkpoint 属于 durable/runtime
  权威，不由通用 Executor 代管。
- **Durable Workflow**：`workflow_submit` 只负责 admission/creation；独立
  `DurableWorkflowWorker -> WorkflowRuntime` 每个 quantum 最多提交一个 work item 结果或一个局部
  plan revision。语义 work item 通过 `AgentGraphRuntime.run_work_item()` 回到同一 assistant loop；
  work-item Tool allowlist 是可信空集合也必须表示“暴露零个 Tool”，不能退化为完整 Registry。
  `deep_research` work item 固定使用空本地 Tool allowlist；联网由 Qwen/Bailian Chat Completions 的
  Provider-native 搜索完成，不注册或调用本地 `web_search`/`web_fetch`，也不会绕过 Tool 治理链。
  每次 work-item run 回传实际 model/tool call 数并在同一 revision commit 中扣减预算；后续 quantum
  在 model、workflow quantum 或 deadline 耗尽时终止。Tool 预算为零时不再暴露 Tool，剩余预算同时
  收窄 work-item assistant loop 的 iteration 上限。
- **Memory**：记忆读写遵循 `MemoryPluginHost` 与 Plugin lifecycle；默认长期记忆不是主模型可调用 Tool。
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
- `workflows/`：Workflow 契约、definition、Store、LangGraph controller、worker、artifact/context 和
  `AgentGraphRuntime` work-item adapter；
- `tools/plugins/builtin/workflow/`：`workflow_submit` Tool 与 fail-closed Plugin；
- `api/routes_workflows.py`：identity-scoped status/events/input/cancel 薄入口。

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
