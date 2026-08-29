# LangGraph-native Assistant 运行与流式架构

最后更新：2026-08-29

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 统一生产 Assistant、预装子 Agent 与原生 stream 的当前权威 |
| Owns | 统一 Agent 拓扑、Memory middleware、标准 messages、task state 边界、原生 stream/interrupt/checkpoint |
| Does not own | Agent Server HTTP 生命周期、Tool schema、Memory 后端、媒体 wire、Provider 凭据 |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/assistant_agent.py`、`native_agent/memory_middleware.py`、`native_agent/state.py`、`runtime/local_backend.py`、`runtime/thread_resources.py` |
| 验证入口 | `docs/authority.toml` 中 `runtime-event-stream.verification` |
| 相邻 authority | Agent Server 见 [`agent-server-architecture.md`](agent-server-architecture.md)；Tool 见 [`tool-calling-architecture.md`](tool-calling-architecture.md)；视觉能力见 [`visual-perception-architecture.md`](visual-perception-architecture.md) |

## 统一生产图

每个用户会话直接运行一个 `AssistantAgent`，没有外层 wrapper graph，也没有公开或内部的 fast、planning、coding 模式路由：

```text
AssistantAgent
  -> MemoryLifecycleMiddleware.before_agent (recall)
  -> direct answer | write_todos | task(precompiled roles) | tools | filesystem | execute
  -> MemoryLifecycleMiddleware.after_agent (delayed extraction refresh)
```

公开 Graph input 只有标准 `messages`。公开 `AssistantRunContext` 只有 `enable_memory`，默认 true，同时控制本轮
recall 与 delayed extraction。身份、入口和视觉 capability 只存在于 Agent Server
签发的 namespaced metadata，不是 Assistant 配置。主图不绑定 saver；thread、run、checkpoint、interrupt、resume、
cancel 和 Store 均由 Agent Server 注入。

`AssistantAgent` 由 Deep Agents `create_deep_agent` 编译，直接拥有官方 Todo、filesystem、同步 `task`、
summarization、HITL 与 `ToolNode`。简单请求可直接回答；复杂请求可由模型自主使用 `write_todos`。主 Agent 的
业务 Tool、本机文件 Tool 与 `execute` 在同一个模型循环和 Tool surface 中，不再切换另一张 coding 子图，
也不保留项目自研 planner、coding StateGraph、proposal/review/repair ledger 或 execution router。

主 Agent 的同步 `task` 只选择受信 composition 预装的 `general-purpose`、`coder` 和 `browser-operator`，不能在
task 参数中创建 Tool、backend 或权限。`description` 只帮助模型选择角色，不参与授权。`general-purpose` 使用编译好的
worker，task 输入做显式 allowlist 投影，只传一条任务 `HumanMessage` 和冻结的 `memory_context`；输出只返回最终非空
`AIMessage` 以及存在时的 `structured_response`。父级 Todo、Tool Profile、async task、Provider search profile 和未知未来
state 不进入 worker，worker 的内部 transcript 与私有 state 也不回灌父级。若两种有效输出都为空，投影返回有界失败报告。

`general-purpose` 只接收 composition 明确传入的查询与分析 Tool，并使用只实现 `ls/read_file/glob/grep` 的
`ReadOnlyHomeBackend`；同步和异步形态复用同一 worker graph，异步形态固定为
`general-purpose-background`。`coder` 由 Deep Agents 原生 declarative SubAgent 装配，只继承主 backend 的 filesystem
与 `execute`，不接收业务或浏览器 Tool。`browser-operator` 只接收已发现的 Playwright Tool，filesystem 仅以只读方式
映射当前 thread 的 `/scratch/` 与 `/artifacts/`，不获得宿主 filesystem 或 shell。`coder` 与 `browser-operator` 当前只支持同步 task；所有子 Agent
都不装配 async delegation Tool，避免递归委派。

## 本机 filesystem、thread 资源与后台 worker

主 Agent 使用 `CompositeBackend`。默认 `HomeShellBackend` 以 `/home/lenovo1/assistant_agent` 为 cwd，
Deep Agents 传入的 `/.` 映射到该 cwd，`/` 和其他绝对路径保持宿主 OS 语义。
`/artifacts/`、`/scratch/`、`/uploads/` 是上下文快捷路由。同步/异步 `general-purpose` 使用
`ReadOnlyHomeBackend`。Skill discovery 使用另一份独立 `FilesystemBackend`，只由
`SkillsMiddleware` 读取产品内建 Skill。

生产没有 Workspace 或 project registry。thread 只在 Agent Home 下拥有摘要命名的 `scratch/`、`uploads/` 和
`artifacts/` 临时目录；主 Agent 直接操作 Agent Server OS identity 有权访问的真实路径。Git 仓库按当前操作路径通过
`git -C <path> rev-parse --show-toplevel` 动态识别，不复制仓库、不创建 detached worktree，也不提供 patch 回灌 Tool。
worker thread/run 必须来自进程内 async adapter 签发的 internal capability；普通外部身份即使伪造完整 metadata 也会被拒绝。

internal capability 当前是进程内随机 secret，适配本地单进程部署且不会写入 state、thread/run metadata、日志或
配置文件。多进程 Agent Server 启用前必须改为共享 secret 或正式 service identity。

主图以标准 messages 为事实源，只增加冻结的 `memory_context/memory_status` 与按 task ID 合并的 `async_tasks`；
这些字段通过官方 schema metadata 从公开 input 隐藏。
Tool Profile 和递归步数属于 middleware 私有 state。当前生产图是 `assistant-native-v4`；retired native v1/v2/v3
和 worker-v1 的 thread/checkpoint 只能检查或 drain/cancel，不能进入 v4 run/resume/replay/stream。

## HITL 与执行边界

所有需要审批的具体 Tool 名由受信 composition 直接传给 Deep Agents `interrupt_on`。filesystem 的
`write_file`、`edit_file`、`delete`、`execute` 固定进入审批；业务、MCP、browser 与异步 Tool 使用各自显式的
auto-approve/interrupt 名单，不依赖 Tool metadata 分类。interrupt 在 handler 执行前产生，恢复统一使用 Agent
Server/LangGraph 的原生 resume。

HITL 是审批治理，不是进程或文件系统隔离。当前 `HomeShellBackend` 继承官方 `LocalShellBackend`；
用户批准 `execute` 等价于允许 Agent Server 的 OS identity 在 Agent Home cwd 下执行完整 command。command 仍可能访问
宿主路径、网络和 Git。受信本地单用户开发可以使用该 backend；多租户或不可信生产必须替换为 thread-scoped
container 或 remote sandbox backend。

## 原生流与视觉边界

生产消费者直接使用 Agent Server 的 messages/updates/custom/values 和原生生命周期协议。`AssistantAgent` 是顶层
graph；主模型 token 不再依赖 subgraph stream，媒体入口仍可启用 subgraph stream 以接收并过滤内部 task worker，且只投影标准 assistant
正文；同步 task 与只读 worker 的内部消息、Tool 参数和 ToolMessage 正文不进入媒体 wire。

`ToolProgressMiddleware` 通过原生 custom stream 发送 `tool_name`、`tool_call_id` 和
`started|completed|failed`，不发送参数、结果或异常正文。模型循环不设置 model 或单 Tool 的 run 累计次数上限；
同一 model superstep 内同名 Tool 最多并行 12 次，并在 `recursion_limit` 只剩 8 步时关闭 Tool 完成一次自然综合。
main 与 worker 的官方 summarization 从同一 `ProviderConfig` 取得窗口、trigger/target ratio 和可选离线 token counter；
real DeepSeek/native compactor 缺 tokenizer 时 composition 启动失败。

实时摄像头的进程级并行流水线与 namespaced capability facts 仍会运行和冻结；SigLIP2
latest-wins、关键帧窗口和并行 VLM 始终由视觉 authority 负责，不进入主图 state 或 task。但当前 media custom route
的 `media_graph_input()` 只投影文本，不把 `source=live_camera` block 注入标准 message；因此依赖
`latest_runtime_media(...).live_video_ids` 的实时视觉 Tool 不会由该入口条件暴露。恢复这些能力需要另立受信的非消息投影
和 coverage，不得让主 LLM 直接感知摄像头。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/tdd/unified-assistant-agent
```
