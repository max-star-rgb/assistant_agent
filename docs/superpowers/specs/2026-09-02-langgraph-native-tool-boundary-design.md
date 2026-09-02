# LangGraph 原生 Tool 边界全量重构设计

日期：2026-09-02
状态：书面规格已批准，等待实施

## 1. 文档定位

本文是“项目物理架构重构总纲”切面三的 Tool 原生化总设计，覆盖全部项目自研生产业务 Tool。
它属于开发设计材料，不替代当前 authority；生产行为仍以 `AGENTS.md`、`docs/authority.toml`、
`docs/tool-calling-architecture.md`、各领域 authority、源码和测试为准。

本设计解决的是业务 Tool 虽已由 `ToolNode` 执行，但 handler 与领域代码仍依赖自研 `ToolContext`、`ToolResult`
和 `invoke_native_tool` 的过渡状态。目标不是重写 Tool 功能，而是让生产执行边界和依赖方向完整采用
LangChain/LangGraph 原生协议。

## 2. 当前事实

当前生产主链已经是：

```text
BaseTool -> ToolNode -> ToolRuntime[AssistantRunContext] -> ToolMessage
```

项目现有 17 个自研业务 Tool：

- 日历与联系人：`calendar_search`、`calendar_create`、`contacts_search`；
- 邮件：`email_search`、`email_read`；
- 本机与网络：`local_file_read`、`web_fetch`；
- 电商与住宿：`shopping_search`、`lodging_search`、`hotel_price_watch_create`；
- 生成：`image_generation`、`image_to_3d`；
- 媒体：`uploaded_media_inspect`、`live_view_inspect`、`visual_memory_search`、`visual_reminder_manage`；
- 视觉图片搜索：`visual_image_search`。

其中 15 个业务模块导入 `ToolResult`，12 个导入 `ToolContext`，14 个通过 `invoke_native_tool` 执行业务回调。
这些类型和函数均为项目自研：

- `ToolContext` 从原生 `ToolRuntime` 复制 identity、thread/run、metadata 和旧 cancellation/trace 字段；
- `ToolResult` 把领域数据、模型投影、trace、audit、voice 和旧 handoff 字段混在统一 envelope；
- `invoke_native_tool` 执行业务回调，再把 `ToolResult` 转为 `content/artifact` 或 `ToolException`。

`git`、`activate_tool_profile` 和 `deactivate_tool_profile` 已直接使用原生 Tool 输入输出，不依赖上述兼容链。
Deep Agents filesystem、Todo、task、async task 与官方 MCP Tool 本身也是原生能力，不进入重写范围。

## 3. 目标与硬约束

完成后，全部项目自研生产业务 Tool 必须满足：

```text
@tool / BaseTool
  -> ToolRuntime[AssistantRunContext]
  -> Tool adapter 提取可信运行事实
  -> 领域 request/backend/service
  -> 领域 result
  -> 原生 content/artifact 或 ToolException
  -> ToolNode 生成 ToolMessage
```

硬约束：

- `ToolRuntime` 是业务 Tool 唯一运行时注入入口，隐藏参数不得进入模型 schema；
- Tool 名称、description、输入 schema、availability、Profile、retry 与 HITL 行为不变；
- identity 只从 `AssistantRunContext.server_info.user.identity` 读取；
- 条件暴露只使用入口、媒体、环境和服务端事实，不增加关键词或正则意图路由；
- 所有副作用 Tool 继续使用原生 HITL，外部幂等继续归具体业务 API 或 Tool；
- 成功返回标准 `content/artifact`，失败使用有界 `ToolException`；
- Provider 原始响应、secret、宿主绝对路径和未清洗异常不得进入模型 content 或 artifact；
- 不新增 Registry、Executor、ToolSpec 镜像、通用领域执行器或第二套 Tool schema；
- 不改变 mock/real Provider 安全边界，不调用真实 Provider。

## 4. 采用方案

采用“同一总设计、按能力垂直批次迁移”的方案。每批都直接达到目标形态，不建立新旧双轨框架；全部批次完成后，
生产 Tool 链不再引用 `ToolContext`、`ToolResult` 或 `invoke_native_tool`。

不采用：

- 只迁移媒体 Tool：不能完成切面三的全量原生化目标；
- 一次机械替换全部文件：容易把权限、HITL、幂等和媒体窗口差异抹平；
- 为所有 Tool 定义统一 `DomainResult` 或 service interface：不同领域输出和错误语义不同，统一抽象只会复制
  `ToolResult` 的问题；
- 只移动目录但保留 `tools` 反向依赖：不会改善真实依赖方向。

## 5. 统一原生边界

### 5.1 Tool handler

每个 handler 继续由官方 `@tool(..., response_format="content_and_artifact")` 定义，并直接接收
`ToolRuntime[AssistantRunContext]`。handler 只负责：

- 模型可见参数与 Pydantic 校验；
- 从 runtime 提取可信 identity、thread/run、state、cwd、媒体 capability 和 idempotency key；
- 构造现有领域 request 或调用现有窄 backend/service；
- 将领域结果投影为有界模型 content 和结构化 artifact；
- 把已知领域错误转换成安全 `ToolException`。

`ToolRuntime` 不传入领域 backend/service。领域实现也不导入 `assistant_agent.tools.runtime`。

### 5.2 输出与错误

不新增全局结果 envelope。优先复用各插件已有 Pydantic request/result model；简单 read Tool 可直接使用后端返回的
结构化 dict。模型 projection 保留在 Tool adapter，完整领域数据进入 artifact。

`tools/native_boundary.py` 可以保留纯函数型的 JSON content/artifact 投影、metadata、错误清洗和原生 idempotency key
辅助，但不得执行传入的业务 callback。`invoke_native_tool` 从所有生产 Tool 移除后删除。

已知输入、Provider 和业务错误在 handler 边界转换为 `ToolException`；`configure_builtin_tool` 继续设置
`handle_tool_error` 和 metadata。未预期错误必须先安全清洗，不向模型回显原始异常、请求或凭据。

### 5.3 `ToolContext` 与 `ToolResult`

生产 Tool 不再构造 `ToolContext`。backend/service 改为接收：

- 已有领域 request；或
- 最少的显式 `user_id`、`session_id`、`run_id`、cwd、媒体窗口和 idempotency key。

生产 Tool 不再构造 `ToolResult`。旧 Runtime state、历史 observability 或非生产代码若仍引用该类型，留到切面四按
实际消费者迁移或删除；它们不得阻止本切面移除全部生产 Tool 依赖。只有全仓最后一个真实消费者消失后才删除类型定义，
不建立兼容 re-export。

## 6. 领域和物理归属

### 6.1 轻量集成 Tool

日历、联系人、邮件、网页、本机文件、购物、住宿和视觉图片搜索已经有各自的 `models`、`backend` 或 `adapter`。
这些实现只有单一插件消费者时继续放在对应插件包，不为了目录整齐新建十个顶层领域包。

Tool 文件只保留 handler；业务参数转换、外部 client 调用和领域结果继续由现有 backend/adapter 承担。
backend/adapter 不接收 `ToolContext`、`ToolResult` 或 `ToolRuntime`。

### 6.2 生成与 durable task

图片生成和 3D job 已有 `media` 领域实现；Tool 只负责运行事实、审批后的调用和生成物 content block 投影。
酒店价格监控继续调用 `automation.durable_tasks` 的 service 契约，Tool 不拥有 task 状态机。

`calendar_create`、`image_generation`、`image_to_3d` 和 `hotel_price_watch_create` 的 HITL 名单与幂等规则保持不变。

### 6.3 媒体 Tool

`tools/plugins/builtin/media_inspection/video_branch.py` 迁移为
`media/video/understanding_service.py`，`VideoUnderstandingBranch` 重命名为 `VideoUnderstandingService`。
它复用现有进程级视觉资源，不创建第二个 client、store 或视觉流水线。

上传视频、实时 exact-target、视觉记忆和视觉提醒的领域逻辑归 `media`。Tool adapter 只校验入口签发的可信 capability、
构造领域 request，并投影领域结果。Media service 不导入 `assistant_agent.tools`。

## 7. 实施批次

### 批次一：简单只读 Tool

迁移：

- `calendar_search`、`contacts_search`；
- `email_search`、`email_read`；
- `web_fetch`、`local_file_read`；
- `shopping_search`、`lodging_search`、`visual_image_search`。

本批次建立直接 `ToolRuntime -> backend -> content/artifact` 模式，不改变业务结果。

### 批次二：副作用与生成 Tool

迁移：

- `calendar_create`；
- `image_generation`；
- `image_to_3d`；
- `hotel_price_watch_create`。

本批次重点验证原生 HITL、幂等 key、生成图片 content block、异步 job handoff 和 durable task 创建语义。

### 批次三：媒体 Tool

迁移：

- `uploaded_media_inspect`；
- `live_view_inspect`；
- `visual_memory_search`；
- `visual_reminder_manage`。

同时把视频理解业务编排迁入 `media`，保持视觉并行流水线、exact-target 和连接级 reminder 状态机不变。

### 批次四：兼容链收口

- 确认所有生产 Tool 不再导入 `ToolContext`、`ToolResult`、`invoke_native_tool`；
- 删除 `invoke_native_tool` 和仅为生产兼容链存在的 projection 逻辑；
- 按剩余消费者判断 `tools/runtime.py`、`tools/models.py` 中旧类型是删除还是登记到切面四；
- 更新 Tool、视觉、Durable Task 与相关 authority 的 owner 和 source globs；
- 不保留仓库内部 import shim。

## 8. 测试策略

每个批次先在独立 `tests/tdd/langgraph-native-tools/` 中建立 RED，再修改生产代码。测试至少证明：

- 每个业务 Tool 是标准 `BaseTool`；
- `ToolRuntime` 不进入模型可见 schema；
- 成功调用产生预期 content 和 artifact；
- 已知错误产生有界 Tool error，不泄露原始异常；
- backend/service 不接收或导入 `ToolContext`、`ToolResult`、`ToolRuntime`；
- 副作用 Tool 的 `interrupt_on`、审批跳过条件和 idempotency key 不变；
- 条件 Tool 的 availability 与服务端事实校验不变；
- 媒体 exact-target、视觉记忆和 reminder 行为不变；
- Provider-backed Tool 在 mock 模式不初始化或调用真实 Provider。

每批运行受影响定向测试；最终运行 `tests/core`、全部相关临时 TDD、Ruff、compileall、authority validator 和 8089
hot reload。若当前没有对应永久 core invariant，不为目录迁移机械新增永久测试。

## 9. 验收标准

- 17 个自研业务 Tool 全部由 `@tool/BaseTool + ToolRuntime` 直接实现原生执行边界；
- 生产 Tool 源码中 `ToolContext`、`ToolResult`、`invoke_native_tool` 引用数为零；
- Tool backend/service 中 `ToolRuntime` 和 Tool compatibility 类型引用数为零；
- Tool 名称、schema、Profile、availability、retry、HITL 和模型可见结果保持契约兼容；
- 领域数据继续进入 artifact，模型只看到有界 projection；
- Media、Automation 和 Provider 依赖方向不再反向指向 Tool 执行类型；
- 没有新增 Registry、Executor、通用 DomainResult、空接口或长期兼容 shim；
- 受影响定向测试、`tests/core`、静态检查和 authority validator 通过；
- 8089 服务成功 hot reload 并加载三个生产 Graph；
- 不调用真实 Provider。

## 10. 非目标

- 不重写 Deep Agents filesystem、Todo、task、async task 或官方 MCP Tool；
- 不改变 Agent Graph、Tool Profile、Provider 选择、Memory lifecycle 或媒体 wire；
- 不改变视觉算法或串行化并行视觉流水线；
- 不借本切面重写业务 API、Prompt 或产品返回文案；
- 不为未来 Tool 预建通用 service framework。
