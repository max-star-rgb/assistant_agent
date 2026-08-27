# LangGraph-native Assistant 运行与流式架构

最后更新：2026-08-27

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant 父图、fast/planning 子图与原生 stream 的当前权威 |
| Owns | 父图拓扑、模式路由、标准 messages、create_agent、planning/coding super-step、原生 stream/interrupt/checkpoint |
| Does not own | Agent Server HTTP 生命周期、Tool schema、Memory 后端、媒体 wire、Provider 凭据 |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/`、`src/assistant_agent/coding/workspace.py` |
| 验证入口 | `docs/authority.toml` 中 `runtime-event-stream.verification` |
| 相邻 authority | Agent Server 见 [`agent-server-architecture.md`](agent-server-architecture.md)；Tool 见 [`tool-calling-architecture.md`](tool-calling-architecture.md)；视觉能力见 [`visual-perception-architecture.md`](visual-perception-architecture.md) |

## 生产运行图

用户会话仍只有一个 `AssistantRootGraph`；独立生命周期后台任务由 Agent Server 注册的兄弟图
`assistant-worker-v1` 承载，不是父图静态边：

```text
AssistantRootGraph
  -> memory_recall
  -> execution_router
       fast     -> AssistantFastAgent --------+
       planning -> AssistantPlanningAgent ----+
       coding   -> AssistantCodingAgent ------+
  -> refresh_memory_extraction
  -> END
```

fast 与 planning 都通过 Deep Agents `AsyncSubAgentMiddleware` 获得
`start/check/update/cancel/list_async_task(s)` 五个标准 Tool。五者静态注册到 `ToolNode`，但统一归入独立
`async-tasks` Tool Profile，只有调用 `activate_tool_profile` 后才进入当前 invocation 的模型可见 schema。
`start_async_task` 创建独立 worker thread/run，立即把
task handle 写入父 thread 的 `async_tasks`；handle 同时保存 task/child thread/child run ID 与原始 parent thread/run ID，
当前回复无需等待 worker 完成。planning 原有同步 `task` 保留，供计划内部
必须等待结果的工作使用；后台 worker 不装配异步 middleware，避免递归 delegation。

公开 Graph input 只包含标准 messages。`execution_mode` 是不可变 runtime context，只允许
`fast|planning|coding` 并默认使用 `fast`；`enable_memory` 默认为 true，
关闭时同时跳过该 run 的 recall 与 delayed extraction。同一
`assistant-native-v3` graph 的自建 Assistant 保存持久 context，单次 run 可用同名稀疏 context 覆盖。
`execution_router` 把合并并校验后的 context 冻结进 state。路由函数不从用户文本、关键词、Tool 或 Memory
推断模式。父图不绑定 saver，
由 LangGraph Agent Server 注入 checkpoint、thread、run、cancel、resume 与 Store 资源。

Studio 可在同一 graph 上创建 owner-scoped Assistant；公开 context 只有 execution preset 与 Memory 开关。
fast 与 planning coordinator 共用分层 Prompt Builder：稳定核心规则不可覆盖，用户北京时间/真实地区和本轮媒体事实
随后追加；入口 profile、实时媒体模式与视觉 capability 不属于公开 Assistant schema。

coding 分支直接装配 Deep Agents 0.7.8 官方 `create_deep_agent` 编译出的
`AssistantCodingAgent`。进程装配时把当前项目固定为内部 repository；项目的薄 `SandboxBackendProtocol` adapter
使用 Agent Server 认证 identity、当前 thread ID 与内部 repository ID 复用 `CodingWorkspaceService`，
把每个会话映射到独立 detached Git worktree，再把文件和
命令操作委托给 Deep Agents 官方 `LocalShellBackend`。

Coding Agent 使用 Deep Agents/LangChain 官方 middleware 和 Tool：`write_todos`、`ls`、`read_file`、
`write_file`、`edit_file`、`delete`、`glob`、`grep`、`execute`、`task`、summarization 与
基于 `RemainingSteps` 的递归耗尽前自然综合。它不设置 model 或单 Tool run 累计次数上限；同一 model superstep 内
同名 Tool 最多并行调用 12 个实例，后续 turn 可再次调用任意参数。repository 文件虚拟根始终是当前 thread worktree。Deep Agents
`HumanInTheLoopMiddleware` 对 `write_file`、`edit_file`、
`delete` 和 `execute` 发出原生 interrupt，checkpoint 与 resume 继续由父图和 Agent Server 所有。

当前实现面向本地入门开发：`LocalShellBackend` 以 worktree 为 cwd、文件 Tool 使用 virtual path 防止路径穿越，
且不继承宿主环境变量；但 shell 本身不是容器安全边界。部署到不受信输入或多人生产环境前，应把同一
`SandboxBackendProtocol` adapter 替换为 thread-scoped container/remote sandbox。当前没有专用或无人审批的
commit、merge、push、PR 流程；但用户批准 `execute` 后，命令仍可能访问宿主路径和网络并执行这些 Git 操作，
system prompt 不是确定性安全边界。

生产不再装配项目自研 coding StateGraph、coding Tool inventory、patch proposal/digest approval、并行 analysis/review、
validation/formatter/repair loop、dependency/credential/artifact gate 或 controlled Git integration。需要这些能力时优先
使用 Deep Agents 官方 sandbox、HITL、subagent 与 middleware 扩展，而不是恢复第二套 Agent Runtime。

`memory_recall` 只召回长期记忆，并在同一次节点更新中写入 `memory_context` 与 `memory_status`；recall 最终失败时，
原生 error handler 将 Memory 标记为 `degraded`。日期与地区不属于 Memory，也不进入 Graph state/checkpoint：fast 与
planning 在每次 model call 使用原生 `dynamic_prompt`，把北京时间自然日和真实用户地区配置追加到 system prompt 末尾。
从 recall 后的 interrupt 恢复会沿用冻结 Memory，但 system prompt 会按恢复时的北京时间自然日重新生成；不提供时分秒。

fast 与 planning 直接作为父图节点装配。fast 分支是 `create_agent` 编译出的唯一共享
`AssistantFastAgent`，使用标准 `BaseChatModel`、`BaseTool`、`ToolRuntime`、messages channel 和官方
middleware，不维护项目自建 assistant/tool loop。
两者在配置的正常 model call 预算耗尽后，通过原生 middleware 再执行一次显式绑定 `tool_choice="none"` 的无 Tool 真实模型调用；该调用复用已有
messages 与 Tool observation 生成最终答复，不使用人工 `AIMessage` 终止。

planning 分支是官方 `create_agent` 编译出的 `AssistantPlanningAgent`：

```text
model -> tools -> model
          task -> AssistantFastAgent
model -> END
```

Supervisor 通过官方 `TodoListMiddleware` 获得可执行 `write_todos` Tool，通过 Deep Agents
`SubAgentMiddleware` 获得可执行 `task(description, subagent_type)` Tool，并通过只绑定 Skill 虚拟根的上游
`read_file` 在拆解前读取专项知识；它不持有 `activate_tool_profile` 或业务 Tool。Todo 的
`content/status=pending|in_progress|completed` schema、更新语义和
执行逻辑均由锁定的 `langchain==1.3.15` middleware 提供；项目只通过其官方扩展参数提供中文 system prompt 与
Tool description，不再维护 Todo reducer、completed gate 或 Worker result ledger。Supervisor 固定关闭
Provider-native search。

`task` 是 Deep Agents 0.7.8 提供的真实 `StructuredTool`，不是路由占位 schema。唯一注册的
`general-purpose` 类型直接引用已经编译的共享 `AssistantFastAgent`。task 用 description 创建子 Agent 的唯一
`HumanMessage`，同时传递父 planning state 中冻结的 Memory 与 execution mode。Planner 是否读取
Skill 仍由 LLM 自主决定，并把 task 所需规则写入 description；Skill metadata、读取 transcript 和加载状态不进入子
Agent。父 conversation、Todo、Tool Profile、调用计数和 structured response 也不进入子 Agent。子 Agent 可按自身原生
循环读取 Skill、激活 Tool Profile、调用业务 Tool、
summarize 或触发 planning 模式非 read HITL。完成后 Deep Agents 只把 structured response 或最后一条非空
`AIMessage` 文本写成原 task call 对应的父级 `ToolMessage`；项目结果投影不回灌 worker Skill/Profile state 或内部
AI/Tool transcript。

同一 `AIMessage` 中的多个 task call 由 `create_agent` 内置 `ToolNode` 并行执行；fan-out/fan-in、Tool 错误、
`Command` state update 与 checkpoint 都使用上游实现。项目只为 task 并发回写的冻结字段声明“结果必须一致”的
LangGraph reducer；Planner 与 worker 的 Skills middleware state 和文件读取 transcript 保持角色局部，不合并为
Tool Profile、权限或能力授予。
主链也不再维护 controls、`Send(worker)`、join、wave、attempt、reservation 或 recovery ledger。


## State 与恢复

生产 state channel、checkpoint 和 reducer 调度全部使用 LangGraph 原生能力。父图继续以标准
`AgentState.messages` / `add_messages` 为事实源，并只增加从 runtime context 冻结的 `execution_mode`、
冻结的 `memory_context/memory_status` 与后台 task handle `async_tasks`。该 task map 使用按
task ID 合并的 reducer，因此 fast/planning 模式切换后仍可检查、更新或取消同一任务；后续 worker run 沿用 handle 中
冻结的原始 parent thread/run ID。fast agent 子图只额外保存显式
`activate_tool_profile` 产生的 `active_tool_profile_ids`；上游 `skills_metadata` 是 middleware 私有 state，Skill 正文只
存在于当前角色的标准 `read_file` transcript。

planning agent 只在官方 state 中保存标准 `messages`、`todos`、冻结的 Memory、execution mode 和
上游 middleware 私有的 `skills_metadata`；它不保存或激活 Tool Profile，也没有项目 Skill/grant channel。
Todo 不含项目 `todo_id`，也没有 `worker_results` 或 `worker_writes`
channel。task 调用、结果与 Todo 更新都作为标准 AI/Tool transcript 进入 checkpoint；子 Agent 私有 transcript
不进入父 state。恢复、并行 Tool pending writes 与错误语义均由 `create_agent`/`ToolNode`/Agent Server 所有。

coding agent 的 checkpoint 使用 Deep Agents 官方 state：标准 `messages`、Todo、filesystem 与
subagent middleware 私有字段。项目不保存 proposal、validation、analysis/review snapshot、repair ledger 或 integration
decision channel；认证 identity、thread 与 workspace 宿主路径只在 backend 调用时解析，不写入模型可见 state。

父图不投影或改写生成图片。`image_generation` 直接使用标准 `ToolMessage(content, artifact)`：模型下一次调用
只读取窄文本 `content`，程序消费者从 `artifact.images[]` 读取受管图片引用。最终 `AIMessage` 保持模型原始
回答，因此 Studio 当前只显示生成成功文本，不承诺图片预览；媒体 WebSocket 在入口适配层完成自己的 wire 投影。

实时摄像头 chat 只通过最新标准 `HumanMessage` 的 `source=live_camera` video block 进入父图；其中可以携带
视觉模块生成的可信目标边界，但 JPEG、Provider client、task 和 lease 不进入 state。父图只通过标准 ToolNode
消费视觉结果，逐帧并发、等待和晚到结果语义见视觉 authority。

当前原生 planning state 与已删除的 A-lite planning checkpoint 不兼容；已存在的 A-lite v3 planning thread
不做 state migration，Studio 与客户端必须新建 thread。生产 Graph 继续使用版本化
`assistant-native-v3`。Agent Server auth 按 graph-aware create 把 chat thread 的 metadata `assistant_graph_id`
规范为 v3，同时保留独立 Memory graph identity；chat run-create 与显式 graph metadata update 以 owner + v3 identity
过滤，旧 identity 不能通过更新伪装升级。旧 run 的 interrupt/rollback 只按 owner 授权，以便部署时 drain/cancel。
SDK adapter 还会在 create/stream 边界复核相同 identity。v1/v2
或缺失 identity 的 unknown thread 及其 checkpoint 只读，不能进入 v3 run/resume/replay。部署前必须 drain 或
cancel v2 pending/interrupt run；completed 历史可 inspection。校验函数仍接收调用方期望的 graph ID，
不阻止 Memory 等独立 Graph 使用自己的 thread 与版本身份。


完整 Tool inventory 仍静态注册给 fast/planning `create_agent` 的 `ToolNode`；通用 `ToolProfileMiddleware` 自带
`activate_tool_profile`，每次 model call 的可见子集由受信静态 profile catalog 与当前 invocation 的激活状态派生。
fast catalog 包含业务 Profile 与 `async-tasks`，planning catalog 只包含 `async-tasks`，后台 worker 不装配后者。
Skill 只提供渐进知识，不参与 Tool 授权；未归属 profile 的 Tool 保持独立可见。该过滤不创建第二套 Tool runtime，
也不改变 ToolNode 对已注册 Tool 的标准执行路径。

回答生成后，主图通过官方 Agent Server SDK 在由 chat thread 确定性派生的 companion Memory thread 上查询
pending runs，只对带 `assistant_agent_run_kind=memory_extraction` metadata 的旧 Memory run 执行
`cancel(..., action="rollback")`，随后立即 enqueue 新 delayed Memory run；chat thread 不保留后台 pending run。
Memory 重试、error handler 和失败后的 `Command(update=..., goto=...)` 均是 LangGraph 原生 node 扩展能力，
不是项目自研降级层。chat run 的 recall 重试耗尽后 handler 写入显式 `memory_status=degraded` 并跳到
`execution_router`；回答后的 refresh 失败只结束当前主图，不丢弃已经生成的回答。独立 Memory Graph 失败只影响后台 run。
项目只声明“Memory 是辅助能力，因此失败仍继续”这一产品结果。

## 原生流与生命周期

生产消费者直接使用 Agent Server 的 messages/updates/custom/values、thread/run、cancel、checkpoint、interrupt 与
resume 协议。媒体入口只订阅 messages/values，不消费
updates/custom。模型 token、Tool 消息和节点 state update 由 LangChain/LangGraph 原生 callback/stream 产生；项目不再投影
`GraphStreamPart`、`AgentEvent` 或产品 run 状态作为主链事实源。
父图中的 fast/planning 单元是子图，因此需要模型 token 的消费者必须显式启用原生 subgraph stream；媒体入口
仍只把标准 assistant 文本和受控兼容投影发送到 wire，不转发 planner、Tool 参数或 ToolMessage 正文。
Tool 执行通过官方 runtime stream writer 向 custom mode 发送 `tool_progress` 生命周期事件；只包含
`tool_name`、`tool_call_id` 与 `started|completed|failed`，不包含 Tool 参数或结果正文。由于 fast/planning
执行单元是父图子图，需要完整协议的 Agent Server SDK/API 消费者分别请求
`stream_mode=["messages", "updates", "custom"]` 并设置 `stream_subgraphs=True`；进程内调用
`graph.astream(...)` 时使用相同的 `stream_mode`，并设置 `subgraphs=True`。`stream_mode` 选择事件类型，subgraph
开关决定嵌套 namespace 是否可见，两者互不替代。
planning 不定义专用 recovery custom event。Graph Studio 的固定父级路线显示
`planning_agent -> model/tools`；每次具体执行路线由 `task` Tool 的嵌套 subagent run、messages/updates 和 LangSmith
trace 展开，不再显示固定 controls/worker/join 节点。媒体 custom route 不订阅或重解释 planning 内部事件，也不
建立 shadow event bus。


`HumanInTheLoopMiddleware` 使用 state-aware policy：fast 模式自动放行本地业务 Tool，planning task 内的
fast 子 Agent 对非 read Tool 触发原生 interrupt；coding agent 对 `write_file`、`edit_file`、`delete` 和
`execute` 始终触发 Deep Agents 官方 HITL。恢复统一使用 Agent Server/LangGraph `Command(resume=...)`。

coding agent 的 checkpoint 使用 Deep Agents 官方 state：标准 `messages`、Todo、filesystem 与 subagent
middleware 私有字段。项目不保存 proposal、validation、analysis/review snapshot、repair ledger 或 integration decision
channel；认证 identity、thread 与 workspace 宿主路径只在 backend 调用时解析，不写入模型可见 state。

评测侧继续直接调用生产父图；旧 CodingGraph 专项 runner 不再代表当前行为，后续能力评审应围绕官方 Deep Agent 的
filesystem、HITL、sandbox 与最终仓库结果建立。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/tdd/deepagents-coding-agent
```
