# 内建 Tool 官方原生化迁移设计

日期：2026-08-18

## 背景与目标

当前生产执行链已经使用 LangChain `BaseTool -> ToolNode`，但多数内建 Tool 仍继承项目自定义
`ToolBase`。`ToolBase` 会从领域 Request 动态生成一层包含 `ToolRuntime` 的严格 Pydantic
`args_schema`，同时裁剪 runtime-owned 和服务端默认字段。该双 schema 机制导致模型看到的 JSON
Schema 与执行时校验不完全一致：例如 `lodging_search` 对外声明 `string/format=date`，Provider
按 JSON 协议返回日期字符串后，执行层却因 `strict=True` 只接受 Python `date` 对象。

本次迁移将全部内建 Tool 改为 LangChain 官方 `@tool` 定义，删除 `*Tool` 类构造 API、自定义
`ToolBase`、动态 schema 生成和声明式 runtime input binding。迁移后仍保留既有 Plugin 静态装配、
领域 Pydantic Request/Result、adapter/service、安全策略和标准 `ToolMessage(content, artifact)`。

## 范围

### 纳入范围

- 将当前 20 个 `ToolBase` 子类改为 `create_*_tool(...) -> BaseTool` 工厂。
- 复用现有 3 个媒体 `@tool` 工厂模式，并使全部内建 Tool 使用一致的官方定义方式。
- 同步修改 Plugin、生产 inventory、system eval、core probe、临时 TDD 和包导出。
- 将 Tool 运行时边界所需的公共逻辑拆为不参与 schema 生成的普通函数。
- 删除不再使用的 `ToolBase`、动态 `_native_input_model`、`RuntimeInputBinding` 及其旧绑定函数。
- 更新 `docs/tool-calling-architecture.md`，使 authority 与源码一致。

### 不纳入范围

- 不改 MCP：MCP Tool 已由官方 `MultiServerMCPClient` adapter 生成。
- 不改 Provider-native 联网能力与本地 Tool 的边界。
- 不重写领域 adapter、Provider HTTP wire、durable task 状态机或媒体业务服务。
- 不调用真实 Provider；迁移验证全部使用 mock/local/offline。
- 不保留旧 `*Tool` 类的兼容别名或双轨构造入口。

## 方案选择

采用“每个 Tool 一个官方 `@tool` 工厂”的方案：

```python
def create_example_tool(adapter: ExampleAdapter | None = None) -> BaseTool:
    backend = adapter or MockExampleAdapter()

    @tool(EXAMPLE_TOOL_NAME, response_format="content_and_artifact")
    def example(
        query: Annotated[str, Field(min_length=1)],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        request = ExampleRequest(query=query)
        result = execute_example(backend, request, tool_context(runtime))
        return native_tool_response(EXAMPLE_TOOL_NAME, result)

    example.metadata = {"effect": "read", "source": "builtin"}
    return example
```

不采用动态裁剪 Pydantic schema 的公共 Tool 工厂，因为它会重新形成第二套 schema 生成机制；也不
直接手写 `BaseTool._run/_arun` 子类，因为这会保留大量框架样板，无法获得官方 `ToolRuntime` 参数
自动隐藏与注入的主要收益。

## 组件边界

### Tool 工厂

每个现有 Tool 模块保留业务辅助函数，删除 `*Tool` 类，增加具名 `create_*_tool` 工厂。工厂闭包持有
进程级 adapter、service、store 或安全配置；内部 `@tool` 函数只暴露模型可以决定的参数。

Plugin 继续作为可信静态 composition：读取 `ToolPluginContext`，判断 Provider 和资源是否就绪，调用
具体工厂并返回 `list[BaseTool]`。Plugin 不解析用户文本、不执行 Tool 业务，也不生成 schema。

### 领域模型与业务实现

现有 Request/Result Pydantic model 和 adapter/service 保留。原生 Tool 函数从显式参数和可信 Runtime
事实构造完整 Request，再调用普通业务函数。这样 wire schema 与领域 schema 不必强行共享同一个模型，
但日期、URL、枚举和跨字段约束仍由领域模型完成最终校验。

### Runtime 边界

保留 `ToolContext` 作为 adapter/service 内部上下文值对象，但将它与 `ToolBase` 解耦。新增或整理普通
runtime helper，用于：

- 从 `ToolRuntime.server_info.user.identity` 取得唯一受信用户身份；
- 从 `runtime.execution_info` 取得 thread/run/tool-call 信息；
- 从标准 messages 取得最新真实用户请求与媒体引用；
- 从 state 读取本轮冻结的 `memory_context` 和 `skill_reference_grants`；
- 为需要幂等的写 Tool 生成稳定、调用级幂等键；
- 构造 adapter 所需的 `ToolContext`。

这些 helper 不创建、复制或修改 Tool schema。

### 输出边界

`ToolResult` 继续作为 Tool 业务层的统一内部结果。一个普通边界函数执行以下转换：

- `success=True`：将有界 `model_observation` JSON 序列化为 LangChain content block，并把完整、受治理
  的 `data` 作为 artifact 返回；
- `success=False`：抛出 `ToolException`，使 `ToolNode` 按官方错误路径产生 ToolMessage；
- 未知异常：经 `sanitize_error_message` 脱敏后包装为 `ToolException`；
- 已有 `ToolException`：原样传播。

图片、视觉等需要特定 content block 的 Tool 可以保留本模块的投影逻辑，但仍返回官方
`content_and_artifact` 协议。

## 参数所有权

### 模型可见参数

只有 LLM 可以根据用户请求决定的业务参数出现在 `@tool` 函数签名。参数通过 `Annotated`、`Field`、
枚举或嵌套 Pydantic model 直接生成官方 Tool JSON Schema。

### 服务端参数

`limit`、`max_chars`、`max_total_chars`、`timeout_s`、图片生成数量/尺寸/seed 等服务端控制参数不进入
函数签名；它们来自工厂参数或领域 Request 默认值。LLM 不可覆盖。

### Runtime-owned 参数

`runtime: ToolRuntime[AssistantRunContext]` 由 LangChain 自动注入并从模型 schema 隐藏。用户身份、
session/run、Memory、Skill grant、请求媒体和幂等键在函数体内从 runtime 构造，不再声明
`runtime_input_bindings`。

特别处理：

- `calendar_create`：基于 thread、run 和 `tool_call_id` 构造调用级幂等键；
- `image_generation`：注入受信 user/session 与冻结 Memory；
- `visual_reminder_manage`：注入当前 thread 作为 session，并从 server info 取得用户身份；
- `hotel_price_watch_create`：闭包直接持有已装配的 `DurableTaskService`，不依赖未注入的 metadata；
- `load_skill_reference`：从当前 state 的 `skill_reference_grants` 校验 reference 授权。

## 治理元数据

全部内建 Tool 至少写入：

```python
tool.metadata = {
    "effect": "read" | "generate" | "write" | "dangerous",
    "source": "builtin",
}
```

条件暴露 Tool 额外写入闭合枚举 `availability`。HITL、read retry、MCP 合并和条件暴露继续只消费
标准 `BaseTool` 与 metadata。

旧 `repeat_policy`、`requires_media` 和 `trace_content_policy` 当前没有生产消费者，不迁移为私有属性；
若未来出现真实需求，应先定义标准 metadata 或 middleware 契约，而不是恢复自定义 Tool 基类。

## 错误处理与安全

- Pydantic/函数参数错误由官方 Tool 调用路径处理，不增加第二次 wire schema 校验。
- 领域 Request 仍执行跨字段和业务格式校验，包括住宿日期顺序。
- Python sandbox 安全校验在原生 Tool 函数执行前显式调用；不保留未统一调用的 `validate_call` hook。
- 本地文件路径归一化、根目录逃逸和扩展名白名单保持在 Tool-owned 执行边界。
- 外部 adapter 失败继续产生有界、可解释的 Tool 错误；Provider 原始响应不进入模型上下文。
- 写入和生成 Tool 的 `effect` 不变，planning HITL 行为不得因迁移而改变。
- mock/real Provider 门禁保持不变。

## 迁移策略

采用分批、始终可验证的迁移：

1. 先建立官方 Tool 公共 runtime/output helper，并将 core probe 改成 `@tool`。
2. 迁移简单只读 Tool：email、web、website、local file、contacts、calendar search、lodging search。
3. 迁移带安全或写入语义 Tool：Python、calendar create、hotel watch、visual reminder。
4. 迁移带 Runtime/Memory 和生成能力 Tool：image generation、image-to-3D、shopping、visual image search。
5. 迁移 Skill Tool 并验证渐进暴露的 `Command`/grant 行为。
6. 同步 Plugin、system eval 和导出后，删除旧基类与绑定实现。
7. 更新 authority 并执行全量离线验证。

每批迁移均先编写失败测试，观察 RED，再实现 GREEN；不得先批量改完再补测试。

## 验证与完成标准

### Core invariant

`TOOL-001` 保持语义但更新测试实现：core probe 本身使用官方 `@tool`，验证 runtime 不进入模型 schema、
身份只来自 `server_info.user.identity`，并由 `ToolNode` 产生标准 `ToolMessage(content, artifact)`。

`EXT-001` 增加结构化断言：静态内建 inventory 全部为 `BaseTool`，名称唯一，且具有合法的
`effect/source` metadata。core 测试不导入具体业务 Tool。

### 临时 TDD

在 `tests/tdd/native-builtin-tools/` 覆盖：

- 全部工厂的工具名、公开 schema 和服务端/Runtime 参数隐藏；
- ISO 日期字符串经真实 `ToolNode` 成功进入 `lodging_search`；
- 身份、Memory、Skill grant、媒体和幂等注入的代表性路径；
- read/generate/write/dangerous effect 保持；
- 业务失败、未知异常脱敏与标准 artifact；
- Plugin 在 mock/unconfigured/ready 条件下的 inventory 装配。

### 静态与集成检查

- `rg` 确认生产、测试、eval 不再引用 `ToolBase`、`RuntimeInputBinding` 或旧 `*Tool` 类；
- 运行 `tests/core/contract/test_tool_contract.py`、`test_extension_contract.py` 和受影响的 core 集成测试；
- 显式运行 `tests/tdd/native-builtin-tools/` 及现有受影响 TDD；
- 运行 Ruff、文档 authority validator 和 `git diff --check`；
- 等待现有 8089 `langgraph dev` hot reload，并验证服务 schema/health；
- 不调用真实 Provider。

完成时，生产内建 Tool 只存在官方 `@tool` 工厂定义，旧类构造 API 和双 schema 运行路径完全删除，
所有既有治理能力通过标准 Runtime、metadata、ToolException 与 ToolMessage 保持。
