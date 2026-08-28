# 统一 Assistant Agent 设计

日期：2026-08-27

状态：已确认，待实施

本文是开发阶段设计材料，不是当前生产 authority。实现完成后，以同步更新的
`docs/*.md` authority、源码和测试为准。

## 1. 背景

当前 `AssistantRootGraph` 在 Memory recall 后按 `execution_mode=fast|planning|coding`
路由到三个执行子图。三者都以模型和 Tool 的原生循环为核心：fast 直接调用 Tool，planning
在 Todo 与 `task` 协调后复用 fast，coding 使用 `create_deep_agent` 提供 Todo、文件、Shell
和子 Agent。它们的主要差异已经收敛为 Prompt、Tool 集、workspace backend 与 HITL 策略，
而不是不同的推理流程。

这种拆分带来重复的 Graph 装配、state、测试、客户端模式字段和 checkpoint 版本负担，也把
“规划行为”“代码能力”“快速直接回答”错误建模成互斥模式。目标架构改为一个 Agent 自主决定
直接回答、调用 Tool、维护 Todo 或委派子 Agent；所有副作用由运行时审批边界治理。

## 2. 目标与非目标

### 2.1 目标

- 将 fast、planning、coding 合并为一个 `AssistantAgent`。
- 使用 Deep Agents 原生 `create_deep_agent` 作为唯一生产执行循环。
- 简单请求允许模型直接回答，不要求创建 Todo、调用 Tool 或委派子 Agent。
- 复杂请求由同一 Agent 自主使用 `write_todos` 和 `task`。
- 仓库文件与 Shell 能力绑定到认证用户和 thread 对应的隔离 Git worktree。
- 所有文件写入、命令执行、业务副作用、非只读 MCP 和生成类 Tool 在执行前触发原生 HITL。
- 同步 `general-purpose` 子 Agent 与后台 worker 只读，主 Agent 是唯一写入者。
- 删除公开 execution mode、客户端 mode 字段和三分支路由。
- 明确本地 Shell 只适用于受信的单用户开发环境；多租户或不可信输入的生产部署必须使用远端或容器
  sandbox。

### 2.2 非目标

- 不实现只读 Plan mode。
- 不新增自研 Agent loop、scheduler、Tool executor 或 checkpoint adapter。
- 不为子 Agent 创建独立写入 worktree，也不实现自动合并。
- 不把本地 `LocalShellBackend` 描述为容器安全边界。
- 不新增 `host_execute`；当前原生 `execute` 已在宿主机运行。
- 不迁移旧 Graph checkpoint。

## 3. 总体架构

生产父图保持 Memory 前后处理，只删除模式路由：

```text
AssistantRootGraph
  START
    -> memory_recall
    -> AssistantAgent
    -> refresh_memory_extraction
    -> END
```

`AssistantAgent` 是唯一执行子图：

```text
AssistantAgent
  ├─ 直接生成回答
  ├─ business tools
  ├─ filesystem tools
  ├─ execute
  ├─ write_todos
  ├─ task -> AssistantReadOnlyWorker
  └─ async task tools -> assistant-worker-v2
```

父图继续由 Agent Server 注入 checkpoint、Store、thread、run、cancel、interrupt、resume 和
stream 生命周期。Memory recall 与 delayed extraction 的现有语义不变。

## 4. Agent 装配

### 4.1 主 Agent

主 Agent 通过一次 `create_deep_agent` 构造，长期名称固定为 `AssistantAgent`，父图节点名为
`assistant_agent`。装配内容包括：

- 当前 Provider 的标准 `BaseChatModel`；
- 受信静态业务 `BaseTool` inventory 与 MCP allowlist；
- `CodingWorkspaceBackend`；
- 项目 Skills 来源 `/skills/`；
- 显式覆盖的 `general-purpose` 只读 CompiledSubAgent；
- 现有异步 delegation adapter；
- 项目仍需保留的 Tool Profile、条件 Tool 暴露、Memory context、retry、调用限制、最终综合和
  Tool progress middleware；
- 文件、命令、业务写入、危险、生成和非只读 MCP Tool 的统一 HITL 配置。

主 Agent 不包含要求先规划或必须委派的规则。模型认为已有信息足够时直接回答；只有复杂任务需要
状态跟踪时才使用 `write_todos`，只有隔离上下文确有收益时才调用 `task`。

### 4.2 原生能力优先

以下能力只使用 `create_deep_agent` 的原生装配，不再由项目重复安装：

- `FilesystemMiddleware` 与 `ls/read_file/write_file/edit_file/delete/glob/grep`；
- `SubAgentMiddleware` 与 `task`；
- summarization；
- Tool-call patch middleware；
- backend 提供的 `execute`。

项目只追加上游没有覆盖的业务治理 middleware。`TodoListMiddleware` 只追加一次。不得同时保留
`build_fast_agent`、`build_planning_agent` 和 `build_coding_agent` 三套 factory。

## 5. 文件、worktree 与 Shell

### 5.1 单一模型文件视图

不使用 `CompositeBackend`。模型只看到一套原生文件 Tool，全部通过
`CodingWorkspaceBackend` 访问当前 thread 的 Git worktree。Skill、`AGENTS.md`、源码、测试和文档
都是该文件系统内的普通文件；`read_file` 不为 Skill 建立特殊权限或 Tool。

`SkillsMiddleware` 的内部目录发现固定使用进程项目目录，避免简单聊天只因枚举 Skill 就创建
worktree。该 backend 只供 middleware 发现 metadata，不向模型注册另一套 Tool。模型实际调用的
`read_file` 始终使用 worktree backend；首次真实文件或命令操作才惰性解析或创建 worktree。

### 5.2 worktree 边界

`CodingWorkspaceBackend` 按 Agent Server 认证 identity、thread ID 和进程固定 repository ID 调用
`CodingWorkspaceService.resolve()`，每个 thread 获得独立 detached Git worktree。文件 Tool 保持
`virtual_mode=True`，不得通过 `..`、`~` 或绝对路径越出虚拟根。workspace 创建失败必须 fail closed，
不得回退到主项目目录。该约束只适用于 `ls/read_file/write_file/edit_file/delete/glob/grep` 等文件
Tool，不适用于 Shell 进程。

### 5.3 `execute`

只保留 Deep Agents 原生 `execute`。当前 backend 委托 `LocalShellBackend`，因此命令作为宿主机进程
运行，cwd 固定为当前 thread worktree。它既可运行 Git、Python、pytest 与构建命令，也可在用户批准后
运行 `systemctl`、`docker`、`apt` 或设备命令。

Git worktree 只隔离文件修改，不隔离进程、网络或宿主系统资源。所有 `execute` 调用都必须在执行前
展示完整命令并触发 HITL；子进程只获得受控的最小环境变量集合，不自动继承 Provider key、token、
`.env` 或其他 secret。命令设置有界超时和输出上限，非零退出码、超时与输出截断作为结构化结果返回。

`virtual_mode=True`、worktree 和 `cwd=worktree` 都不是 `execute` 的 confinement。命令仍能以 Agent
Server OS identity 读取该身份可访问的任意宿主机文件、访问网络、修改系统并消耗宿主机 CPU、内存和
磁盘；最小环境变量只能减少继承的 secret，不能阻止命令直接读取宿主机上的 secret。

**HITL 是 `execute` 的治理边界，不是进程隔离边界；在 `LocalShellBackend` 下，批准 `execute`
等价于批准模型以 Agent Server OS identity 执行该完整 shell command。**

因此 `LocalShellBackend` 只允许用于受信的本地单用户开发环境。面向多租户、不可信用户输入或生产
Web/API 的部署，远端或容器 sandbox 是生产安全 requirement；此类部署若仍装配
`LocalShellBackend` 必须 fail closed，不能只依赖 HITL、worktree 或 `virtual_mode` 通过安全检查。

如果未来把默认 backend 替换为容器或远端 sandbox，`execute` 将随 backend 在该隔离环境内运行。
只有届时仍存在明确的宿主机控制需求，才新增独立 `host_execute`。

## 6. Tool 暴露与审批

### 6.1 Tool Profile

- `task`、`write_todos` 保持核心可见。
- 文件 Tool 与 `execute` 归入 `filesystem` Profile，按现有
  `activate_tool_profile` 机制渐进暴露。
- 业务 Tool 继续使用现有受信静态 Profile catalog。
- 异步 task 生命周期 Tool 继续归入独立 Profile。
- Skill 只提供知识，不授予 Tool 权限。

Profile 激活只控制模型 schema 与上下文体积，不是授权边界。未激活 Tool 抵达执行边界时继续返回
可恢复的标准 error `ToolMessage`，不得进入 handler。

### 6.2 HITL

以下调用在主 Agent 中始终审批：

- `write_file`、`edit_file`、`delete`；
- `execute`；
- `effect=write|dangerous|generate` 的本地业务 Tool；
- 所有非只读 MCP Tool。

用户可 approve、edit 或 reject。审批策略由 middleware 和 Tool metadata 决定，不从用户文本、Skill、
Prompt 或模型判断中推导。内部惰性 worktree 创建与只读 Tool 不审批。

## 7. 子 Agent 与后台 Worker

### 7.1 同步 `general-purpose`

显式覆盖 Deep Agents 默认 `general-purpose` 子 Agent，使用进程内唯一编译的
`AssistantReadOnlyWorker`：

- 只提供 worktree 的 `ls/read_file/glob/grep`；
- 只提供 `effect=read` 的业务 Tool；
- 不提供文件写入、`execute` 或其他副作用 Tool；
- 不提供同步或异步 delegation，禁止递归；
- 使用独立的窄 `AssistantReadOnlyWorkerState`，只声明 `messages` 和 `memory_context`；
- 接收 task description 与冻结 `memory_context`，不能看到父 Todo、Profile、内部 transcript 或其他
  state；
- 只向父级返回最后一条非空 AI 文本或 structured response，不回写其他 state。

并行 task 因为全部只读，不会发生共享 worktree 写冲突。主 Agent 汇总结果并承担所有写入。

单写者约束同时建立在两层上：

1. **Model tool surface**：Worker 的 `FilesystemMiddleware` 使用
   `tools=["ls", "read_file", "glob", "grep"]`，模型看不到 `write_file`、`edit_file`、`delete` 或
   `execute`。
2. **Backend capability**：Worker 使用最小 `ReadOnlyCodingWorkspaceBackend(BackendProtocol)`，只把
   `ls/read/glob/grep` 转发到 worktree 内 `virtual_mode=True` 的文件 backend；不实现或转发
   `write/edit/delete/upload_files`，也不实现 `SandboxBackendProtocol`，所以没有 `execute` 能力。即使
   Tool allowlist 因上游变化失效，意外抵达的写调用也必须 fail closed。

主 Agent 不自定义 `FilesystemMiddleware`，由 `create_deep_agent` 根据主 backend 生成默认实例；审批由
顶层 `interrupt_on`/HITL 配置完成。Worker 因需要缩小 Tool surface 才显式提供原生
`FilesystemMiddleware(tools=[...])`，但其安全性不依赖 top-level filesystem permissions 被复制到该
实例。

这是针对 Deep Agents 当前开放问题
[`#5388`](https://github.com/langchain-ai/deepagents/issues/5388) 的显式兼容边界：自定义同名
`FilesystemMiddleware` 会替换 factory 默认实例，而当前公开构造器无法接收 factory 使用的
`_permissions`，可能让 deny/read-redaction 规则静默丢失。本项目不读取私有 `_permissions`、不 patch
上游，也不复制一套文件 Tool；通过原生 Tool allowlist、无写能力 backend 和结构化回归测试守住单写者
invariant。上游修复后可以删除问题注释，但不能删除这两层 invariant。

#### 7.1.1 State isolation

Deep Agents 当前 `SubAgentMiddleware` 使用排除列表构造子 Agent 输入：替换 `messages`，移除 `todos`、
`structured_response` 与 upstream 已知 private state，其余父 state 默认复制给子 Agent。CompiledSubAgent
返回后，middleware 还会把结果中未被排除的 state 字段合并回父 state。因此仅声明窄 Worker schema 或依赖
upstream 当前排除列表，都不能构成本项目的 state isolation 契约。

在 `SubAgentMiddleware` 与 `AssistantReadOnlyWorker` 之间保留一个窄 `RunnableLambda` 投影边界。它使用
正向 allowlist，而不是排除列表：

```text
Parent task state
  ├─ messages                  → upstream 替换为 task description → pass
  ├─ memory_context            → pass
  ├─ memory_status             → drop
  ├─ provider_search_profile   → drop
  ├─ async_tasks               → drop
  ├─ active_tool_profile_ids   → drop
  ├─ todos                     → drop
  ├─ skills/middleware state   → drop
  └─ 未来新增的任意 state       → drop by default
```

投影后的 Worker 输入精确为：

```python
{
    "messages": [HumanMessage(content=task_description)],
    "memory_context": parent_memory_context,
}
```

`memory_status` 是父运行的运维状态，Worker Prompt 不消费它，因此不传。认证 identity、thread、callback 与
trace metadata 仍按 LangGraph config/runtime 的受信路径传播；它们不是模型可见 state，也不由这条投影复制。

Worker 完成后的结果使用第二个正向 allowlist 投影：

```text
Worker result
  ├─ messages             → pass，交给 upstream 转为父 task ToolMessage
  ├─ structured_response  → optional pass，交给 upstream 序列化为 ToolMessage
  └─ 其他所有 state        → drop by default
```

因此上游 `_return_command_with_state_update` 即使继续支持合并其他字段，也收不到本项目 Worker 的其他字段。
upstream private-state exclusion 只作为额外防线；本项目正确性不依赖其当前字段列表。

### 7.2 后台 Worker

同一个只读 worker 装配注册为 `assistant-worker-v2`，供异步 task lifecycle Tool 创建独立 thread/run。
它继续使用相同模型和受治理只读能力，但不注册异步 task Tool，避免递归 delegation。后台 task handle
继续按 task ID 合并并保存父 thread/run identity。同步 `task` 在父 run 内读取父 thread worktree；异步
worker-v2 使用自己的 child thread worktree，并在 task 创建时固定 repository snapshot：

```text
start_async_task
  -> 从受信 repository 配置读取当前完整 commit SHA
  -> repository_snapshot_sha = abc123...
  -> 写入 task handle 与 child thread/run metadata
  -> worker backend resolve(..., base_commit=repository_snapshot_sha)
  -> git worktree add --detach ... abc123...
```

`repository_snapshot_sha` 是服务端生成的受信 metadata，不是模型 Tool 参数或 Worker state。创建后启动的
每个后续 worker run 都沿用同一个 SHA；`update_async_task` 不得重新读取源仓库 HEAD。现有
`CodingWorkspaceService.resolve()` 增加可选的显式 `base_commit`：主 thread 未指定时保持当前惰性 HEAD
语义，async worker 必须指定。Service 校验它是配置仓库内存在的完整 commit SHA；已有 child workspace
的 `base_commit` 与 metadata 不一致、SHA 缺失或无效时全部 fail closed。

Agent Server 的 worker-v2 thread/run 授权也必须严格校验 namespaced `AssistantRuntimeFacts`：只有
`entry_profile=async_worker` 且包含合法 `repository_snapshot_sha` 才允许创建。Backend 再独立执行同一
fail-closed 检查，避免直接调用 worker graph 或 metadata 宽松解析绕过 snapshot 约束。

因此 task 创建时源仓库为 `abc123`，即使 worker 真正启动前源仓库移动到 `def456`，child worktree 仍固定
在 `abc123`。它不承诺观察父 worktree 中的未提交修改。依赖父 worktree 当前修改的分析必须使用同步
`task`；首版不实现跨 thread diff 复制或 snapshot overlay。

## 8. Prompt、Context 与 State

### 8.1 Prompt

删除 fast、planning、coding 专属角色 Prompt，保留一个分层 Assistant Prompt：

- 稳定 Assistant 核心规则；
- Deep Agents 原生 Tool/规划能力；
- 项目 Skill index；
- 冻结 Memory；
- 当前日期、地区和受信运行事实。

Prompt 说明 Agent 应先理解、再行动、最后验证，但不要求所有请求显式规划。安全权限不依赖 Prompt。

### 8.2 Runtime Context

公开 `AssistantRunContext` 只保留：

```text
enable_memory: bool = true
```

删除 `ExecutionMode` 与 `execution_mode`。认证 identity、thread、repository、入口和媒体 capability 仍由
Agent Server identity 或 namespaced metadata 注入，不进入用户可编辑 context。

### 8.3 State

唯一执行 state 以 Deep Agents `DeepAgentState` 为基础，只增加确有跨 middleware 共享需要的字段：

- `memory_context` 与 `memory_status`；
- `provider_search_profile`；
- `async_tasks`。

Todo、Skill metadata、Tool Profile 与 filesystem/subagent 私有字段继续由对应上游 middleware 声明和拥有。
删除 `FastAgentState`、`PlanningAgentState` 以及生产不再使用的 coding 分支 state。历史开发模块若不在生产
路径且没有其他 owner，随实现删除；不得为了兼容旧设计保留空类型。

`AssistantReadOnlyWorkerState` 不继承上述项目主 Agent state；它只基于标准 `AgentState` 声明
`messages` 与可选冻结 `memory_context`。State schema 用于限制 Worker 自身读写范围，前述
`RunnableLambda` allowlist 则同时保证调用边界的输入与输出隔离，两者缺一不可。

## 9. 错误、恢复与流式

- 用户拒绝审批后产生标准 Tool 结果，Agent 可调整方案或明确交付限制。
- 文件、worktree、业务 Tool、MCP、生成和命令失败均返回有界、可解释结果；未知异常脱敏。
- `execute` 的非零退出、超时和输出截断不得伪装成功。
- async worker 缺少、伪造或无法解析 `repository_snapshot_sha` 时不得回退到当前 HEAD，task 启动失败并
  返回可解释错误。
- 同步子 Agent 失败只对应当前 `task` 调用，不破坏父 checkpoint。
- 同步子 Agent 不得改变父 `provider_search_profile`、`async_tasks`、Todo、Tool Profile 或任何其他非
  messages state；Worker 结果只能形成当前 task call 对应的 `ToolMessage`。
- interrupt、resume、cancel、checkpoint、pending writes 与 stream 继续由 LangGraph/Agent Server 原生拥有。
- Tool progress custom event 继续只携带 Tool name、call ID 与生命周期状态。
- Memory recall/debounce/extraction 的失败与重试语义不变。

由于父图仍将执行 Agent 作为子图，消费者继续启用 subgraph stream 才能看到模型 token。媒体入口只投影
标准 assistant 文本，不暴露 Tool 参数、ToolMessage 或子 Agent 内部 transcript。

## 10. Graph 版本与客户端迁移

本次变更同时改变父图拓扑、context schema、执行 state 和 worker state，采用显式版本升级：

```text
assistant-native-v4
assistant-worker-v2
assistant-memory-v1
```

- 不注册 v3 或 worker-v1 alias。
- 部署前 drain 或 cancel v3/worker-v1 的 pending、running、retrying 与 interrupted run。
- 旧 thread/checkpoint 只读，不允许进入 v4 resume、replay 或新 run。
- 新 Assistant 和 thread 使用 v4 identity；媒体确定性 thread seed 同步使用 v4。
- 所有客户端由项目控制，因此直接删除 `assistantMode` 和 `execution_mode`，不保留接收后忽略的兼容层。
- CLI、media simulator、evaluation target 与 schema 同步删除 mode 参数。

`assistant-memory-v1` 的输入与状态未改变，因此不升级。

## 11. 测试与验证

本次变更修改既有核心不变量，不新增 core invariant：

- `LOOP-001`：三分支路由改为唯一 `AssistantAgent`，且只使用官方 Deep Agent loop。
- `CTX-001`：删除 mode；统一副作用 HITL；同步 task 子 Agent 只读。
- `GATE-001`：生产 graph ID 升级为 v4，后台 worker 升级为 v2。
- `IDENT-001`：公开 Assistant context 只保留 `enable_memory`。

实现采用 `tests/tdd/unified-assistant-agent/` 做临时 RED/GREEN，并更新上述 invariant 已有的 core 测试。
最小结构化验证覆盖：

1. 父图只有 `memory_recall -> assistant_agent -> refresh_memory_extraction` 执行路线。
2. 简单请求可以直接生成标准 `AIMessage`，不强制 Todo、Tool 或 task。
3. 文件 Tool 与 `execute` 解析到认证 identity/thread 对应 worktree。
4. 文件写入、`execute`、业务副作用、生成和非只读 MCP 在 handler 前 interrupt，approve/reject 后只执行一次。
5. `general-purpose` 与 worker-v2 的实际模型 Tool surface 中，filesystem 部分精确包含只读文件 Tool，
   不包含 `write_file/edit_file/delete/execute` 或递归 delegation；其 backend 不满足
   `SandboxBackendProtocol`，直接调用未实现的写能力也 fail closed。该测试不得只断言 Prompt 或
   filesystem permission 配置。
6. 同步 task 输入只包含 task description 与 `memory_context`；即使父 state 带有
   `provider_search_profile`、`async_tasks`、Tool Profile、middleware state 或新增 sentinel 字段，Worker
   也不可见。
7. 即使 Worker 结果包含非消息 sentinel state，父 state 也只新增当前 task 对应的 `ToolMessage`，其余字段
   不回写。
8. async task 在源仓库 `HEAD=abc123` 时创建、worker 启动前将 HEAD 移到 `def456`，child workspace 的
   `base_commit` 仍为 `abc123`；task handle、thread metadata、初始 run 与后续 update run 使用相同
   `repository_snapshot_sha`，缺失或不匹配时 fail closed。
9. Tool Profile 未激活时同时在模型可见性和执行边界 fail closed。
10. v4 context schema 只接受 `enable_memory`，旧 mode 字段被严格拒绝。
11. 媒体、CLI 和 eval 请求不再发送 mode。
12. Memory、标准 messages、stream、async task handle 与 Agent Server 生命周期保持现有契约。

默认使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Provider。实现完成后按
`docs/authority.toml` 的 owner 路由同步更新 runtime、Tool、Agent Server、media、context 和测试 authority，
并运行 documentation authority validator。

## 12. 删除与保留

实现应优先删除重复代码：

- 删除生产 `fast_agent`、`planning_agent`、`coding_agent` factory 与父图三分支路由；
- 删除 `execution_mode` schema、客户端字段和相关测试分支；
- 删除只为三模式存在的 Prompt、state projection 与 compatibility glue；
- 保留并复用 `CodingWorkspaceService`、`CodingWorkspaceBackend`、业务 Tool inventory、Tool Profile、
  Memory、视觉条件暴露、异步 task transport 和 Agent Server 生命周期。

不新增抽象 factory、模式策略层或兼容 adapter。后续只有出现新的真实 backend 路由或远端 sandbox/宿主机
双执行环境时，才引入 `CompositeBackend` 或 `host_execute`。
