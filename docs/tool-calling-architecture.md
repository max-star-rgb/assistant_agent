# LangChain-native Tool 与扩展架构

最后更新：2026-08-17

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产主链 Tool schema、执行、HITL、MCP 与 Provider-native 能力权威 |
| Owns | `BaseTool`、`ToolRuntime` 注入、ToolNode、effect metadata、官方 MCP 装配、Tool middleware |
| Does not own | 父图路由、Memory、Provider HTTP wire、媒体 WebSocket、后台感知和 durable 状态机 |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/tools.py`、`src/assistant_agent/tools/`、`src/assistant_agent/mcp/` |
| 验证入口 | `docs/authority.toml` 中 `tool-calling.verification` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md) |

## 生产边界

生产 Agent 的硬边界是 LangChain 标准 Tool，不再是
`ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。受信 composition 显式列出内建 Plugin，构造既有
具体 Tool；这些 Tool 本身继承 `BaseTool`，不再在生产装配时二次包装为通用 `StructuredTool`。新主链不做
文件扫描、动态 Python module discovery 或 Registry lookup。

每个内建 Tool：

- 模型只看到去除 runtime-owned 字段的 `tool_call_schema`；
- 完整执行 schema 包含 `ToolRuntime[AssistantRunContext]`，由 `ToolNode` 注入当前 state、thread/run、Store
  和 `server_info`；受信用户身份只读取 `server_info.user.identity`，不从 Runtime Context 复制；
- 成功返回标准 `ToolMessage(content, artifact)`；失败抛出 `ToolException`；
- metadata 至少声明 `effect=read|generate|write|dangerous` 与 `source=builtin|mcp`。

只读 Tool 由 `ToolRetryMiddleware` 做有界重试。fast 模式不触发 HITL；planning 模式的非 read Tool 由
`HumanInTheLoopMiddleware` 在执行前产生原生 interrupt。schema、身份与授权仍由具体 Tool/业务 adapter 校验；
外部副作用幂等属于具体 Tool 或业务 API，主链不再维护通用 operation ledger。

## MCP 与 Plugin

外部 MCP 只通过官方 `MultiServerMCPClient` 装配。受信 `MCPServerConfig` 机械转换为 stdio connection；发现后
应用显式 allowlist、read-only effect 和 `<namespace>_<server>_<tool>` 命名。主链不建立 MCP proxy、ToolSpec
镜像或 Registry。MCP tool discovery 属于 worker 进程 composition，只执行一次；schema、history、state 与
run 复用同一个 compiled graph 和 Tool 集合，实际 MCP Tool 调用仍遵循官方按调用创建 session 的行为。

本地 Plugin 只复用纯构造逻辑和 Provider adapter，生产装配清单是代码中的显式列表。旧 Tool CLI、动态
loader、Registry/Executor、离线 MCP server 与 Skill runtime 已删除。两个本地日历 system eval 也通过最小
StateGraph 的标准 `ToolNode` 执行真实 Tool。durable task 不伪造 Agent run：它使用显式工具名/effect allowlist
和窄业务 adapter，缺失 effect metadata 时按可能写入 fail closed。

## Provider-native 能力

Qwen 等模型原生联网参数属于 `BaseChatModel` 请求能力，不伪装成本地 Tool。生产主链的 Qwen/DashScope
OpenAI-compatible 入口直接构造 `ChatOpenAI`，只通过 `base_url`、模型名以及 DashScope 确实支持的
`extra_body` 参数表达差异。流式模式同时启用 usage 回传。联网默认关闭，只有
`QWEN_CHAT_ENABLE_SEARCH=true` 才在 `extra_body` 中启用 `enable_search` 与 `search_options`。
`enable_source`、`enable_citation` 和
`citation_format` 仅属于 DashScope 原生调用风格，不得塞入 OpenAI-compatible 请求。是否调用候选本地 Tool
仍由模型按标准 tool calling 协议决定。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_tool_contract.py \
  tests/core/contract/test_extension_contract.py
```
