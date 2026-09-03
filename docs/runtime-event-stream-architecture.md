# LangGraph-native Assistant 运行与流式架构

最后更新：2026-09-03

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 统一生产 Assistant、预装子 Agent 与原生 stream 的当前权威 |
| Owns | 统一 Agent 拓扑、Memory middleware、标准 messages、task state 边界、原生 stream/interrupt/checkpoint 与主 ChatModel adapter wire |
| Does not own | Agent Server HTTP 生命周期、Tool schema、Memory 后端、媒体 wire、Provider 凭据或媒体 Provider adapter |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/assistant_agent.py`、`native_agent/memory_middleware.py`、`native_agent/state.py`、`providers/dashscope_langchain.py`、`identity.py`、`runtime/local_backend.py`、`runtime/thread_resources.py` |
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

公开 Graph input 只有标准 `messages`。公开 `AssistantRunContext` 包含默认指向 OS 用户 Home 的 `cwd`、默认 true 的
`enable_memory` 与 `require_tool_approval`，以及 Studio 可选的成对绝对 token 压缩阈值；不设置压缩值时沿用服务端
75% 触发、15% 保留。`enable_memory` 同时控制本轮 recall 与 delayed extraction，`require_tool_approval` 允许 Assistant 配置关闭其全部原生 Tool
审批。身份、入口和视觉 capability 只存在于 Agent Server
签发的 namespaced metadata，不是 Assistant 配置。主图不绑定 saver；thread、run、checkpoint、interrupt、resume、
cancel 和 Store 均由 Agent Server 注入。

`AssistantAgent` 由 Deep Agents `create_deep_agent` 编译，直接拥有官方 Todo、filesystem、同步 `task`、
summarization、HITL 与 `ToolNode`。简单请求可直接回答；复杂请求可由模型自主使用 `write_todos`。主 Agent 的
业务 Tool、本机文件 Tool 与 `execute` 在同一个模型循环和 Tool surface 中，不再切换另一张 coding 子图，
也不保留项目自研 planner、coding StateGraph、proposal/review/repair ledger 或 execution router。

主 Agent 以私有 `needs_verification` state 记录待验证状态。成功完成本机写入/执行/Git Tool、显式列入
`verification_tool_names` 的 Tool，或完成 `coder` task 后，主 loop 必须在最终答复前成功调用一次
`task(subagent_type="reviewer")`；普通只读 Tool 和仅列入 HITL `interrupt_tool_names` 的业务 Tool（包括图片生成）不置位。
模型仍可在执行过程中自主调用同一个 `reviewer`；需要验证的 Tool 成功返回后，主 middleware 在下一次主模型调用前注入
reviewer Tool call 并直接跳转到原生 `ToolNode`，确保候选答复不会先于强制审查进入标准 message stream。副作用调用前若剩余 graph step 不足以完成 Tool、reviewer 与最终综合，主 loop
不执行该副作用并直接返回未验证状态；reviewer 模型调用会本地重试一次，连续两次 task 仍失败时 fail closed，避免无限循环。

主 Agent 的同步 `task` 只选择受信 composition 预装的 `general-purpose`、`reviewer`、`coder` 和 `browser-operator`，不能在
task 参数中创建 Tool、backend 或权限。`description` 只帮助模型选择角色，不参与授权。`general-purpose` 使用编译好的
worker，task 输入做显式 allowlist 投影，只传一条任务 `HumanMessage` 和冻结的 `memory_context`；输出只返回最终非空
`AIMessage` 以及存在时的 `structured_response`。父级 Todo、Tool Profile、async task、Provider search profile 和未知未来
state 不进入 worker，worker 的内部 transcript 与私有 state 也不回灌父级。若两种有效输出都为空，投影返回有界失败报告。

`general-purpose` 接收与主 Agent 相同的业务 Tool inventory、filesystem、`execute`、Skills、Tool Profile 与 HITL
配置；同步和异步形态复用同一 worker graph，异步形态固定为
`general-purpose-background`。`reviewer` 是复用主模型的预编译只读 Agent，只接收 task 描述并返回执行结果与验证证据的审查结论；它不装配
Tool、filesystem、Skills 或再委派能力。`coder` 由 Deep Agents 原生 declarative SubAgent 装配，只继承主 backend 的 filesystem
与 `execute`，不接收业务或浏览器 Tool。`browser-operator` 只接收已发现的原生 Playwright Tool，不装配 filesystem
或 shell；Playwright 进程及其截图、下载直接使用当前 run cwd。`reviewer`、`coder` 与 `browser-operator` 当前只支持同步 task；所有子 Agent
都不装配 async delegation Tool，避免递归委派。

## 本机 filesystem、thread 资源与后台 worker

主 Agent 的 backend 在每次调用时从当前 `AssistantRunContext.cwd` 创建原生 `LocalShellBackend`；`.` 映射到该 cwd，
`/`、`/.` 和其他绝对路径保持宿主 OS 语义，不装配 `/artifacts/`、`/scratch/`、`/uploads/` 等虚拟 route。
同步/异步 `general-purpose` 复用同一个 working-directory backend。Skill discovery 使用另一份独立 `FilesystemBackend`，只由
`SkillsMiddleware` 读取产品内建 Skill。

生产没有 Workspace 或 project registry。生成媒体只在 Agent Home 下拥有摘要命名的 `artifacts/generated/` 临时目录；
它只服务媒体 HTTP 交付，不进入 Agent filesystem。Agent Home 不是默认 cwd 或运行副本。主 Agent 直接操作 Agent Server
OS identity 有权访问的真实路径。Git 操作使用独立
`git` Tool Profile：Tool 按 `target_path` 动态识别仓库根并执行参数数组；通用 `execute` 拒绝直接 Git CLI。
生产仍不复制仓库、不创建 detached worktree，也不提供 patch 回灌 Tool。
同步 worker 继承父 run context；异步 worker 的每次 run 显式复制父 `AssistantRunContext`，因此 filesystem、Shell 与
项目指令使用同一个 cwd，但 task handle 仍不保存 Workspace、仓库快照或文件副本。
worker thread/run 必须来自进程内 async adapter 签发的 internal capability；普通外部身份即使伪造完整 metadata 也会被拒绝。

## 物理模块边界

`native_agent/` 是当前生产 Agent 核心，包含统一 Assistant/worker factory、Prompt 与公开 context、原生 state、
Memory middleware、Provider composition、Tool/Profile middleware 和模型循环限制。这里的模块均由当前
`assistant-native-v4` composition 或其 worker/Memory 图消费，不再与旧 Runtime 实现并列分类。

`runtime/` 只保留以下跨领域契约：

- `local_backend.py`、`thread_resources.py`：生产 composition、filesystem 和 thread 资源使用的执行基础设施；
- `chat_adapter.py`、`output_models.py`、`citations.py`、`requests.py`：被 Provider、Context、媒体兼容路径和
  durable task 共同消费的
  provider-neutral 请求与输出契约；
共享 `identity.py` 持有跨 Memory、durable task 与可选 multi-agent 使用的最小 `RequestIdentity` 和默认 agent ID，
生产包不再为该默认值反向依赖可选 multi-agent 协议。

旧 `runtime/state.py`、`cancellation.py`、`capability_grants.py`、state-to-response adapter 与
state-to-observability adapter 已随最后消费者一起删除；
公开兼容 response 仍保留 `tool_calls/tool_results` 字段及空默认，其他 owner 可按自身协议填充。
生产 Tool 的 `ToolRuntime`、`ToolMessage(content, artifact)` 与 `ToolException` 语义由 Tool authority 所有。

Durable task 的 `TaskPlan/TaskStep` 归 `automation/durable_tasks/models.py`；生成媒体、3D job 与主动媒体消息契约
归 `media/`。Observability helper 已归 `observability/`。仓库内部不保留旧 Runtime import shim。

internal capability 当前是进程内随机 secret，适配本地单进程部署且不会写入 state、thread/run metadata、日志或
配置文件。多进程 Agent Server 启用前必须改为共享 secret 或正式 service identity。

主图以标准 messages 为事实源，只增加冻结的 `memory_context` 与按 task ID 合并的 `async_tasks`；
这些项目自定义字段使用官方 schema metadata，仍可由 middleware、Tool 和 checkpoint 读写，但不进入公开
input/output schema。生产终态返回标准 `messages` 及 Deep Agents/LangChain 原生可选字段
`todos/structured_response`，不另造 `final_response` 或顶层 Memory 投影。
`memory_context` 使用 `OmitFromInput + OmitFromOutput`，以便 Deep Agents 仍能按已有显式 allowlist 传给
`general-purpose` worker；其余不得进入任何 subagent 的内部字段使用 `PrivateStateAttr`。
Tool Profile 和递归步数属于 middleware 私有 state。当前生产图是 `assistant-native-v4`；retired native v1/v2/v3
和 worker-v1 的 thread/checkpoint 只能检查或 drain/cancel，不能进入 v4 run/resume/replay/stream。

## HITL 与执行边界

所有需要审批的具体 Tool 名由受信 composition 直接传给 Deep Agents `interrupt_on`。每项审批配置通过原生
`when` 谓词读取 `AssistantRunContext.require_tool_approval`；false 时跳过 interrupt 并直接执行。filesystem 的
`write_file`、`edit_file`、`delete`、`execute` 固定进入审批；业务、MCP、browser 与异步 Tool 使用各自显式的
interrupt 名单，未列入的 Tool 按原生默认执行，不依赖 Tool metadata 分类。interrupt 在 handler 执行前产生，恢复统一使用 Agent
Server/LangGraph 的原生 resume。

HITL 是审批治理，不是进程或文件系统隔离。当前主 Agent 直接使用官方 `LocalShellBackend`；
用户批准 `execute` 等价于允许 Agent Server 的 OS identity 在 run cwd 下执行完整 command。`cwd` 的 Home 内校验只限制
默认启动位置，command 仍可能访问
宿主路径、网络和 Git。受信本地单用户开发可以使用该 backend；多租户或不可信生产必须替换为 thread-scoped
container 或 remote sandbox backend。

## 原生流与视觉边界

生产消费者直接使用 Agent Server 的 messages/updates/custom/values 和原生生命周期协议。`AssistantAgent` 是顶层
graph；主模型 token 不再依赖 subgraph stream，媒体入口仍可启用 subgraph stream 以接收并过滤内部 task worker，且只投影标准 assistant
正文；同步 task 与 worker 的内部消息、Tool 参数和 ToolMessage 正文不进入媒体 wire。

`ToolProgressMiddleware` 通过原生 custom stream 发送 `tool_name`、`tool_call_id` 和
`started|completed|failed`，不发送参数、结果或异常正文。模型循环不设置 model 或单 Tool 的 run 累计次数上限；
同一 model superstep 内同名 Tool 最多并行 12 次，并在 `recursion_limit` 只剩 8 步时关闭 Tool 完成一次自然综合。
main 与 worker 的单一 Deep Agents `SummarizationMiddleware.awrap_model_call` 从同一 composition 投影的 `ChatConfig` 取得窗口、
trigger/target ratio 和可选离线 token counter，并由 Studio 的成对绝对值只覆盖当前 run；
real DeepSeek/native compactor 缺 tokenizer 时 composition 启动失败。

实时摄像头的进程级并行流水线与 namespaced capability facts 仍会运行和冻结；SigLIP2
latest-wins、关键帧窗口和并行 VLM 始终由视觉 authority 负责，不进入主图 state 或 task。但当前 media custom route
的 `media_graph_input()` 只投影文本，不把 `source=live_camera` block 注入标准 message；实时视觉 Tool 由条件 middleware
根据服务端签发的 run-scoped capability 和冻结投影暴露，dynamic prompt 再根据最终可见 Tool 注入规则。
主 LLM 不接收 video ID、sequence 或其他投影内部字段。

Studio/普通 Agent Server 的静态图片和视频继续作为标准 HumanMessage content block 进入 Graph state；
`ConditionalToolExposureMiddleware` 据此暴露 `uploaded_media_inspect`，并只在主模型调用视图中移除上传媒体块。
ToolNode 仍从未改写的 state 取得附件，VLM 结果作为标准 ToolMessage 返回后由同一个主模型完成最终回复；不增加
Graph node、旁路 Runtime 或 VLM 直出终态。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/tdd/unified-assistant-agent
```
