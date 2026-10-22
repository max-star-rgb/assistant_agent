# LangGraph Agent Server 部署架构

本地启动脚本默认监听 `0.0.0.0`（所有 IPv4 网卡），systemd 与 PyCharm 启动配置保持一致。局域网客户端使用宿主机 IP 连接；仅需本机访问时显式传入 `--host 127.0.0.1`。

最后更新：2026-09-08

媒体对外端口为 `8081`，由前台 Gateway 同一 Node 进程的 `media-entry.mjs` 承载 `/agent-service/v1`。
前台一次启动同时在 `0.0.0.0` 监听 `3101` 和 `8081`，两者均提供 `/agent-service/v1`，
任一端口绑定失败则关闭整个前台，停止时同时释放。
音频和字幕协议通过本进程 Gateway 的公开 GCP 会话适配，不经过 Agent Server。
VIDEO 通过本机专用 `/internal/media-visual/v1` 复用既有解码、归档和并行视觉流水线，
后台返回有界 JPEG/画面状态事件，前台送入自己的同一实时会话；该内部连接不创建第二条模型会话。
内部路由仅接受无 Origin 的回环连接，不接收音频或 chat；8081 不暴露业务 REST API。

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant Graph 的 Agent Server 部署、身份和资源生命周期权威 |
| Owns | auth、assistant、thread、run、queue、checkpoint、Store、cancel、stream、custom route 装配与本地部署准备 |
| Does not own | Assistant 节点推理、Tool schema、Memory 后端语义、Media-Agent wire 字段 |
| 源码与 schema 入口 | `langgraph.json`、`src/assistant_agent/agent_server/` |
| 验证入口 | `docs/authority.toml` 中 `agent-server.verification` |
| 相邻 authority | 媒体 wire 见 [`media-agent-service-websocket.md`](media-agent-service-websocket.md)；运行图见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；视觉流水线见 [`visual-perception-architecture.md`](visual-perception-architecture.md) |

## 可运行 Graph 与 Assistant identity

`langgraph.json` 注册三张当前可运行原生 Graph：

```text
assistant-business-v2 -> assistant_agent.agent_server.graph:native_business_graph
assistant-worker-v2 -> assistant_agent.agent_server.graph:native_worker_graph
assistant-memory-v1 -> assistant_agent.agent_server.graph:native_memory_graph
```

Studio/SDK 直接运行 `assistant-business-v2` 业务图，使用业务模型与原生 ToolNode/HITL。
浏览器 Voice 只运行原生 Realtime 会话，通过 `VoiceTasks` 提交独立业务 run。
业务图保留 Memory lifecycle；语音委派显式关闭业务 Memory，语音记忆使用原生会话接入。
同步 task 在原 run 内调用 worker，异步 delegation 使用 worker thread/run；会话关闭后的记忆提取使用 Memory graph。
real 装配仍要求 Realtime 配置和受信百炼搜索完整，mock 不打开真实 Realtime 连接。
Agent Server 拥有业务 assistant/thread/run/checkpoint/interrupt/resume/cancel/Store。
`assistant-voice-v1`、Graph Realtime ChatModel、跨 run WS 池和 `DELETE /voice/sessions/{thread_id}` 已删除。
语音连接由浏览器会话持有并在挂断时关闭，不进入 Graph checkpoint。

run auth 接收的是 Agent Server Assistant UUID，不是 graph ID 字符串。三张系统 Assistant 使用
`uuid5(langgraph_api.graph.NAMESPACE_GRAPH, graph_id)` 的确定性 UUID：

三张系统图为 `assistant-business-v2`、`assistant-worker-v2`、`assistant-memory-v1`。

auth 将这三个 UUID 精确映射到各自 graph。retired `assistant-native-v1`、`assistant-native-v2`、
`assistant-native-v3`、`assistant-native-v4`、`assistant-voice-v1` 与 `assistant-worker-v1` 的系统 Assistant UUID 明确拒绝，不能借旧 identity 创建当前 run。
其他合法 UUID 只可能作为 Agent Server 已持久化的业务 custom Assistant 进入 main graph：custom Assistant 的创建
只能选择业务图；name、description、config、context、版本和 active 状态由原生 Assistant API 持久化。read/search
对普通 API 身份按 owner filter 隔离，三个当前 system Assistant UUID 与 Studio 身份保持可见；update/delete 同样
受 owner filter 治理。run 创建时 Agent Server 会复用 Assistant read filter 校验 custom Assistant 所有权，不能在自己的
thread 上运行其他 owner 的 custom Assistant。auth 不把 custom Assistant 复制成项目配置或另一套 Runtime。

## Thread、checkpoint 与迁移

PyCharm `.run` 配置通过 `scripts/run_pycharm.py` 分别启动后台（`Agent Server (Real)`，8088）、前台（`Realtime Server`，3101），或使用 `Assistant (All)` 一键按后台就绪顺序启动。前台启动等待使用同一身份访问 `/qwen-audio/capabilities`，校验 `search_tool` 为百炼 function schema 或 null，不再要求旧 memory 字段；`/ok` 不代表原生 Graph 已初始化。连接失败和初始化期间的 5xx 在有界时间内重试，认证或路由 4xx 明确失败。启动前停止同目录 systemd 实例；启动器默认自动清理（`--clear-ports` 仅保留兼容旧配置），先停止同项目对应角色或 `all` 的旧启动器，再清理目标端口 TCP 监听进程（后台 8088、前台 3101/8081），SIGTERM 超过 5 秒后使用 SIGKILL，仍未释放则失败。旧 `all` 被停止会同时关闭前后台；其他占用目标端口的程序也会被结束。进程交由 IDE 管理；停止时清理子进程，不自动恢复 systemd。环境文件和常驻服务切换说明见 `scripts/README.md`。

### Qwen Audio Agent 实验入口

V2 媒体连接可通过 `clientCapabilities.audioStreamV1=true` 启用双向音频。
前台 `media-entry.mjs` 作为连接级薄适配器使用本进程 Gateway 的公开 GCP 7 WebSocket，
输入明确为 16kHz PCM16，输出为 24kHz PCM16；不使用旧 Python RealtimeSession，也不新建业务 Runtime。
模型、业务委派和 Memory 继续由既有 Gateway 集成管理；媒体播放回执映射回 Gateway，断线即关闭语音客户端。
本机 personal Gateway 仅对应 `local-developer`；不支持把任意媒体号码映射成独立 Gateway 用户。
不抢占现有浏览器语音会话；8081 未协商的客户端明确失败，8088 旧兼容入口仍保持 ACK 行为。
`media_audio.py` 保留旧兼容桥与新内部视觉通道的 JPEG/画面状态适配；详细合同见媒体 authority 第 5.1 节。

`integrations/qwen_audio_agent/` 锁定 Qwen Audio Agent 2.0.1，通过公开 Gateway 与 BackendPort 扩展接入。
Gateway 承载上游事件驱动的前台 Realtime Agent（不使用 LangGraph），3101 提供 WebUI 和媒体入口，8081 保留媒体专用入口；本项目 Node
集成层负责装配、任务/Memory 协议适配和观测接入，业务委派复用 V2 的 `8088` Agent Server。
Gateway 与 Agent Server 分别承载前台和业务 Agent，生命周期能力不等价；默认 mock 拒绝启动真实语音。
2.0.1 的工具结果回传补丁兼容外部 Tool 省略可选 policy，缺省时沿用上游结果大小限制，避免读取 maxResultBytes 时异常。
前台通用执行事件与 LangSmith 边界见 [`observability-harness.md`](observability-harness.md)。
实时 Agent 通过原生目录配置加载项目 `prompts/PROMPT.md`，基于官方版本直接修改记忆个性化规则；默认人设仍使用官方版本。
项目另保留后台工具描述、时间地点和记忆规则。
原生 memory feature 注入项目 `MEMORY.md`；快照和会话人设仍走原生 builder。具体维护位置见集成 README。
Realtime 对话历史由服务端会话维护；Gateway 不从本地对话记录回灌 `recentMessages`，重连建立新上下文。
本地 UI 历史不作为模型会话历史。文本记忆由 Node 原生 Markdown 模块读写与提取，使用 memory 工具；
不再通过 Mem0 快照桥或 Python Memory Graph；远端视觉快照由独立窄服务通过 `/qwen-audio/visual-memory/snapshot` 提供给前台，不提供模型召回 Tool；当前 Studio/SDK 文本 backend 为 disabled。
前台通过 `/qwen-audio/capabilities` 获取已装配的 `mcp_bailian_bailian_web_search` 原始 schema，并通过认证的 `/qwen-audio/search` 薄桥调用同一个 `voice_search_tool`；不依赖 Memory 开关。工具未配置时不暴露，调用返回明确不可用错误。旧 `web_search`、`fetch_url` 从本项目前台工具面移除，搜索 Provider 和 URL fetcher 均不实例化，不保留旧搜索回退。mock 模式仍拒绝启动真实 Gateway。
真实入口另需 `QWEN_AUDIO_ENABLE_REAL=1` 与完整 key。配置、运行和限制见该目录 README；目前未切换默认入口。

新入口按安装 namespace、受信身份与 Gateway task ID 确定业务 thread；不同任务隔离，采用原生 `reject` 并发策略。
Gateway 集成层按 task ID 从原生 TaskManager 读取来源 session ID，将安装 namespace、认证身份和 session ID
的摘要作为 `qwen_artifact_session` 写入 thread/run metadata，恢复 run 时沿用 thread 上的值。
该字段只关联会话图片产物，不是身份凭据或模型参数；读写仍按认证用户隔离。缺少来源会话或 isolated utility
任务不建立会话产物关联。图片登记与默认图生 3D 规则见 Tool authority，不复用任务 thread 或复制对话历史。
网页与媒体入口可并行运行。锁定 2.0.1 的 `frontend-concurrency-patch.mjs` 将客户端和语音租约从 owner
收窄到 owner + session，保留同会话重复连接与接管治理。媒体内部连接使用随机 session ID；认证用户不变，
长期记忆仍按用户共享。任务自动通知只领取来源会话，不因其他会话有结果而唤醒；音频和关闭提取均按会话隔离。
宿主占用机器麦克风的全局 input suspension 保留，不等同于会话租约互斥。
模型输入只有委派指令，固定 `enable_memory=false`、`require_tool_approval=false`；附件目前明确拒绝。
Adapter 的 `submit` 等待最终结果，前台由上游 Runtime 管理即时受理；SDK `status` 仅查询活动任务，历史可用集成包 task 查询命令对账。
提交结果未知时先核对原生记录，不自动重投。取消指向该任务 thread 内当前 run，关闭 Gateway 只停止观测。

Qwen Audio Gateway 恢复官方前台 runtime 的授权与对话机制；明确请求直接执行，需要确认时由前台交流，
不为每个后台任务增加审批门槛。业务 run 关闭原生 HITL，不再逐工具暂停；也不保留自定义审批工具、
同工具授权缓存、自动 resume 或审批纠正补丁。官方 `respond_permission` 仅在真实授权 capability 存在时暴露，
本 LangGraph adapter 不发布授权请求。输入澄清仍通过原生 input interrupt 与 `respond_agent_input` 续办。
意外遇到旧 HITL 中断返回明确错误，留给 Studio 处理，不自动批准。此变更不调整 Studio/SDK 或独立媒体入口的审批配置。
离线验证不能替代真实语音交互验收。
结果投影只读取本轮成功 ToolMessage 的显式交付声明，校验当前身份/thread 的产物路径与 HTTP 图片类型，
再转为 Gateway artifacts；不解析模型文字中的链接，不扫描目录猜测输出。任务 accepted、执行 completed
与 delivery ready/partial/failed 分开报告；产物暂不可读时明确提示交付失败。
前台不暴露产物重试工具或 `get_current_time`；日期由运行上下文提供。
产物 URL 使用本机 Agent Server origin，当前仅支持同机浏览器；远程客户端需另行提供受控交付链路。
已识别的单个 text-input interrupt 可通过原生 resume 回应；未知 interrupt 报告需 Studio 处理。
`/qwen-audio/*` 为认证本机 Voice 薄桥：Memory 语义由 Memory authority 所有，百炼搜索语义由 Tool authority 所有，不提供通用业务执行入口。

### 独立浏览器语音入口

本机 V2 打开 `http://127.0.0.1:8088/media-lab`，连接后点击“开始持续对话”。浏览器持续上传 16kHz
单声道 PCM16（约 100ms/包），包括静音、模型回复及业务工具执行期间；页面不做 VAD、不按句缓存或提交。
模型配置 `server_vad`（threshold=0.5、silence_duration_ms=800），自动提交输入并创建回复。
Qwen3.5 Realtime 另设置 `idle_timeout_ms=5000`：服务端完成上一段音频播报后，用户持续静默 5 秒由模型原生主动回复；
不在浏览器或服务端另建计时器，不等于把用户停顿阈值改为 5 秒。
浏览器收到 `voice.speech_started` 时立即停止播放、清空音频队列并开始新的字幕轮次。
此入口独立于正式视频通话 `/agent-service/v1`，不修改其媒体处理边界。

`/media-lab/ws` 使用认证身份和随机 UUID 标识语音会话，不创建空的 Graph thread；开始收音后打开原生 Qwen Realtime WS。
为兼容浏览器协议，`session.ready.thread_id` 携带语音 session UUID，不是可运行的 Graph thread ID。
`RealtimeSession` 直接处理 VAD、回复音频、转写和 function call，不将每句话转换为 Voice Graph run，
不经过 ChatModel/ToolNode 语音循环。原始音频不写入 checkpoint；ASR 与回复文本来自 Provider 转写事件，
只用于语音字幕与 Voice trace，不复制到业务输入。

模型完成的工具调用通过 `VoiceTasks` 提交独立 `assistant-business-v2` run，并立即返回 task_id/run_id/accepted。
每个语音会话只创建一个独立业务 thread，后续委派复用该 thread 并设置 `multitask_strategy="rollback"`，
由 Agent Server 回滚冲突中的旧 run，执行最新任务。已完成业务历史遵循原生 checkpoint 语义，不清空整个 thread。
每次 input 只有一条 `HumanMessage(name="voice_agent")` 委派任务；必要背景由 Voice 写入 request。
task_id 是稳定的业务 thread_id，run_id 区分各次执行；业务 trace 以自身输入和原生 loop 开始，不嵌套语音过程。
百炼搜索直接调用受信装配中已注册、无需审批的只读 MCP Tool，暴露原生参数 schema 并返回真实搜索结果；
不创建业务 run，不递增任务版本，也不触发业务 rollback。配置为需要审批的搜索不在原生语音中直接暴露。
任务强制 `require_tool_approval=true`；审批在业务 thread 的 Studio 中处理，语音连接保持可用。
任务状态通过原生 SDK 查询；审批恢复后跟踪该 thread 最新 run。完成结果以独立的外部数据消息通知模型，
不对已返回 accepted 的 call_id 重复提交结果。用户讲话或模型生成时延后通知。
轮询与排队通知携带本地委派版本，替换任务后丢弃尚未发送的旧通知。

语音打断仅停止生成/播放并丢弃旧响应音频；只有新业务委派才触发业务 rollback，讲话本身不触发。
仅显式 `cancel_business_task(task_id)` 调用原生 run cancel(action=interrupt)，不撤销已完成副作用。
挂断只关闭 Provider WS 和状态轮询；已受理任务继续由 Agent Server 管理。页面记录任务编号，
同一身份可以通过任务编号查询；重新连接不自动恢复旧语音上下文或订阅旧任务通知。
`get_business_task` 每次查询最新 run 与完成结果，`cancel_business_task` 等待原生 interrupt 受理；
沿用 Deep Agents 异步工具的“受理后不立即轮询、用户询问时查实时状态、明确要求时取消”语义，保留现有工具名称。
启用 Memory backend 时原生 Voice 额外提供 `recall_memory(request)`，按需读取认证用户的长期背景。
ASR 用户文本在会话关闭后独立提交到 Memory Graph；具体策略见 Memory authority。
业务结果保存在原生 thread/checkpoint；其持久性遵循当前 in-memory Agent Server 的约束。
Provider 协议异常记录 session_id、响应标识、事件阶段及脱敏错误类别，不记录原始参数、媒体或错误正文。
事件接收只关联实际消费的回复增量、工具完成与 response.done；字幕/音频片段完成等辅助通知直接忽略。
无法关联的输出丢弃并记录事件类型，不补造当前轮次、不关闭连接；单次 Provider 请求错误和工具提交错误局部报告。
网络断开、会话初始化失败或资源上限等连接级错误才结束会话。断线提示只列出已确认受理的待查询任务，不声称任务必然正在运行。
业务 run 提交失败保留 task_id 和脱敏 HTTP 状态；不将创建了 thread 等同于已受理，不自动重试不确定的提交。
修改 langgraph.json 的注册图后必须重启启动脚本：脚本使用启动时的临时配置副本，源码自动重载不会更新注册列表。

页面与 WS 仅接受 loopback，WS 校验同源 Origin，不用于公网部署。协议：

- `audio.start` 指定 `sample_rate=16000`、`channels=1`、`format=pcm_s16le`，整个连接只发送一次。
  `audio.started.vad=server_vad` 表示 Provider 会话就绪。结束持续对话关闭连接，不发送逐句 commit。
- 二进制头 13 bytes，小端 `<BId>`：类型 uint8、递增序号 uint32、时间戳毫秒 float64；
  输入类型 1 为 16kHz PCM16，输出类型 3 为 24kHz PCM16。每包最多 200ms，浏览器按序播放。
- `voice.speech_started` / `voice.speech_stopped` 携带 turn_id；`voice.interrupted` 清空播放。
  `voice_input_transcript` / `voice_reply_text` 提供字幕，客户端忽略旧 turn_id 事件；兜底工具前言仍为文字提示。
  `turn.started` / `turn.finished` 标记 Provider response，`turn.error` 报告错误。
- `turn.cancel` 仅停止当前语音回复，继续收音，业务任务不受影响；关闭页面或切到后台结束持续语音并释放麦克风。
- WS 缓冲超过 256KiB 时停止采集；服务端每连接最多 80 消息/秒，空闲 120 秒关闭，控制消息最多 4096 字符。
  单个 Provider 响应事件预算 16MiB，队列有界；持续会话 115 分钟后要求重连。

页面显示已收到的音频时长和播放器状态。“测试扬声器 / 恢复播放”只生成本地提示音，不调用模型。
浏览器开启回声消除和降噪，但真实扬声器回声与设备音量仍需本机验收。
纯采集校验/回环协议保留在 `/media-lab/capture/ws`；验证入口为 `tests/tdd/browser-media/`、`tests/tdd/voice-agent/`。

### 原生会话迁移

项目控制的 thread 只使用 SDK 原生 `metadata.graph_id` 作为 graph identity。thread create、run create 与 SDK stream
边界都复核 owner 和精确 graph identity，thread metadata update 禁止改写该字段。业务 custom Assistant 绑定 main graph identity；
worker 和 Memory 使用自己的 identity。

thread metadata 中服务端签发的 `assistant_agent_runtime` 只允许在 thread create 时写入，创建后保持冻结；任何
identity（包括 internal worker）的 thread metadata update 只要包含该保留 key 都直接拒绝。普通 label update、
不携带 metadata 的 state update 以及 owner-scoped interrupt/rollback 保持可用。

retired native v1/v2/v3/v4、voice-v1、worker-v1 或缺失 identity 的 thread/checkpoint 只读，不能进入业务
run/resume/replay/stream，也不能靠 metadata update 伪装升级。旧 run 的 interrupt/rollback 仍按 owner 开放，供部署
前 drain/cancel。迁移不转换旧 checkpoint；Studio 与 CLI 必须新建业务 thread；旧媒体 chat 暂不开放。

媒体确定性 thread UUID 的 seed 包含当前业务 graph ID，因此同一 connection 重连稳定，但不会命中旧版本 UUID。
`SdkAgentServerClient` 对 `if_exists="do_nothing"` 返回的 existing thread 同样复核 identity，并在 stream 前再次读取
thread；失败时不创建 run、不改变 checkpoint。

## 统一 composition、本机文件系统与 worker

每个 Agent Server worker 进程只创建一个 `AgentServerExecutionOwner`。其 `compose()` 在装配期局部加载并持有完整、
冻结的 `AppConfig`，只向下游投影所需的窄配置段；owner 持有业务模型和窄 VoiceConfig（mock 时为 None）、一次发现的业务/MCP Tool inventory、一个
`MemoryBackend`、一个 `GeneratedArtifactStore`、线程级 MCP session pool，以及编译后的业务、worker、Memory graph。
schema/history/state 请求和 run 都复用该 owner；custom-app lifespan 在进程 shutdown 时关闭一次。
`load_app_config()` 仅位于 composition/入口；Provider、Tool、Memory、Media 叶子只接收显式窄配置，不读取应用配置
环境变量。Provider SDK policy 自身的 `from_env()` 不是应用配置 loader。

本节 main Agent 指业务 Agent，Voice 不持有执行工具。业务 Agent 使用按当前 run 的 `AssistantRunContext.cwd` 创建的 `LocalShellBackend`，并用 Deep Agents 原生
`virtual_mode=False` 接受真实绝对路径；filesystem
将 `.` 映射为 cwd，`/`、`/.` 和其他绝对路径保持宿主 OS 语义。
不装配 `/artifacts/`、`/scratch/`、`/uploads/` 等虚拟 filesystem route。main 和 worker 复用同一个
working-directory backend、基础模型、完整业务 Tool inventory、Prompt Builder、Skills、
Tool Profile、filesystem、`execute` 与 HITL 配置。Skills discovery 仍使用独立 `FilesystemBackend`，只读取产品内建
Skill。worker 不装配同步或异步 delegation middleware，也不运行主 Agent 的 Memory 提取生命周期。
两者的官方 summarization 从同一 composition 投影的 `ChatConfig` 取得 context window、trigger/target ratio 与模型 token counter；
real DeepSeek/native compactor 缺 tokenizer 时在模型 composition 前启动失败。

真实生成图片直接写入当前 `<cwd>/outputs/`，只有一份服务端正式产物；本地图片 mock 直接登记并读取已有源文件，不复制。不再创建 `~/assistant_agent/threads/`。
原生 Agent Server Store 保存产物 owner、thread ID 和文件绝对路径，文件路径不进入模型 observation。
对外统一使用 `/artifacts/{thread_ref}/generated/{filename}`，下载与媒体 resolver 校验身份、登记记录和文件边界。
`thread_ref` 由认证 identity 与 thread ID 的摘要确定；它是引用关联值，不对应磁盘目录。
正式产物不做 thread TTL 删除，MCP/browser 空闲 session 仍独立回收。不存在
`workspace_id`、`workspace.json`、project registry、仓库副本、Git worktree 或 patch 回灌层。Agent 直接操作
Agent Server OS identity 有权访问的真实路径；`git` Tool 按调用提供的目标路径识别仓库根，不预注册或启动时
全盘扫描仓库；通用 `execute` 不执行直接 Git CLI。
Playwright 的 thread session 只复用浏览器进程状态，不创建 thread 文件目录；其进程 cwd 取当前
`AssistantRunContext.cwd`，输出固定写入 `<cwd>/.agent/browser`，空闲 session 独立按 TTL 回收。

`start_async_task` 只把父子 thread/run correlation 写入 task handle、child thread metadata 和 worker run metadata。
只有进程内 async adapter 会在 start/update 的 loopback SDK
请求中附加随机 internal capability；auth 将其转换为 worker-only permission，并同时严格校验 worker metadata。普通
`X-Assistant-User` caller 即使提交形状完整的 async metadata 也不能创建 worker thread/run。业务与 Memory 的
thread/run 明确拒绝 `entry_profile=async_worker` 和 worker-only permission；worker metadata 必须是
`entry_profile=async_worker`。
worker 与媒体 loopback 各自使用当前本地单进程部署的进程内随机 internal capability，不进入 state、thread/run metadata、
日志、`.env` 或仓库；auth 只允许持有媒体 capability 的调用提交 `entry_profile=agent_service`。多进程部署前必须升级为
共享 secret 或正式 service identity。

统一 Agent 的全部副作用 Tool 由官方 HITL 在 handler 前 interrupt。HITL 是审批治理，不是进程隔离；批准
`execute` 等价于授权以 `lenovo1` 运行的 Agent Server 在 run cwd 下执行完整 command，可访问该 OS 用户有权
访问的宿主路径、网络和 Git。filesystem Tool 与 shell 共享这套 OS identity 权限边界。
当前 `LocalShellBackend` 只适合受信本地个人 Agent；多租户或不可信生产必须使用 container 或 remote sandbox backend。

## 公开输入、认证与媒体 custom route

公开 Graph input 使用原生 Agent input schema，只有标准 `messages`：

```json
{"messages":[{"role":"user","content":"hello"}]}
```

Studio 直接提交文字给业务图；浏览器音频只进入原生 Realtime WS，不经 Graph middleware 或 checkpoint。
业务图既有媒体工具保留；旧媒体 chat 入口仍关闭。

公开 `AssistantRunContext` 包含 `persona`、`cwd`、`enable_memory`、`require_tool_approval`，以及可选的
`context_compaction_trigger_tokens` / `context_compaction_keep_tokens`；Studio 的 `cwd` 默认是
`~/assistant_cqy/`，且只接受 Home 内绝对路径。网页、媒体和 Studio/SDK 共用该个人默认工作目录；显式 cwd 与设备绑定继续生效。context coercion 和 Server 启动不创建工作区；文件能力按需创建目录，不创建空 `ASSISTANT.md`。媒体握手的 `context` 复用同一 schema。`persona` 保留 `general` / `call_assistant` 兼容字段；Voice 使用固定 Voice 策略，业务委派固定 general，不授予身份或能力。两个压缩值必须同时设置且 `0 < keep < trigger`；都不设置时业务图使用业务模型窗口的 75%/15%。`require_tool_approval` 在受信本机开发环境默认 false，保存为 true 的
Assistant 才让已配置 Tool 进入 HITL。身份、入口和视觉 capability 只在
服务端签发的 namespaced run metadata 中。认证用户唯一来自 `Runtime.server_info.user.identity`；当前 tokenless
developer hook 从 `X-Assistant-User` 取得 identity，省略时为 `local-developer`，因此端口不得暴露给不受信网络。

`/agent-service/v1` custom route 只做 vendor frame 校验、connection/session/thread/run/delivery 关联、公开 SDK
stream/cancel/join、终态 `AIMessage` 与通用 Tool delivery artifact 投影、视觉引用接入和 callback。它不读取 checkpoint，
不按 Tool 名解析业务 artifact，不执行 Tool/Memory，
不选择 Assistant 模式，也不构造第二套 Runtime。旧 coding behavior attestation route 已删除。

前台 Gateway 关闭时通过原生 ConversationSync 的 pendingRecords 消费本批用户记录，覆盖网页文字和语音；媒体入口仅适配协议到同一路径，不维护独立转写缓存。
前台 Realtime WebSocket 会话关闭后通过官方 `RemoteReflectionExecutor.submit()` 在确定性 companion
thread 上提交 `assistant-memory-v1` 立即执行的 run，使用官方 rollback 替换冲突的 Memory run。普通 chat 使用 Agent Server 原生 `multitask_strategy="interrupt"`；显式
checkpoint replay 使用 enqueue。业务图不再注册 Memory after_agent 或提交提取；提交调度不生成独立 trace，后台提取图保留原生 tracing。主动投递使用独立 Store，不进入 `AssistantAgent` run。

media custom-app lifespan 单次加载 `AppConfig` 后，只保留 provider mode、`VisionConfig` 和 `MediaConfig`。
`REALTIME_VISUAL_ENABLED=false` 默认关闭实时视觉，修改后重启 Agent Server 生效；业务 Agent 和上传附件理解可独立运行。
关闭时不装配实时 embedding/视觉索引、不创建后台感知 observer，也不暴露实时视觉工具。
媒体前台 `VIDEO + audioStreamV1` 可独立解码并向原生 Realtime 会话发送 JPEG，不受该后台开关影响。
`VisualPerceptionModule` 由该 lifespan 持有，通过 `asyncio.to_thread` 完成同步装配（开启实时视觉时含 FastEmbed/Hugging Face
依赖导入和本地模型加载），避免事件循环阻塞检查中断导入并留下不完整模块状态。Graph Tool 只消费其窄接口。媒体接入不得合并、
串行化或删除视觉 authority 定义的 SigLIP2 latest-wins 和并行关键帧 VLM 流水线。

## 本地部署

### 本机目录归属

仓库内 `local/` 与 `data/` 分开保留，均由 Git 忽略：

- `local/`：本机私有配置（如 `mcp_servers.json`）与个人安装笔记；共享模板放 `config/`，共享说明放 `docs/`。
- `data/models/`：可重新下载或导出的模型与缓存，如 SigLIP2 和 FastEmbed。
- `data/calendar/`、`data/agent_server/`：日历、主动投递等持久数据库，需要单独备份，不能当缓存清理。
- `data/visual_perception/`、`data/agent_service_video_frames/`、`data/agent_service_video_keyframes/`：视觉流水线的帧、关键帧和派生数据，按各自生命周期回收。
- `data/remote_visual_memory/`：远端视觉记忆上传暂存，待上传内容不能直接删除；`data/run/` 保存服务运行锁。

真实 `.env` 仍在仓库根目录；官方 checkpoint/Store 仍位于 `.langgraph_api/`。
生成图片只写入用户工作目录 `outputs/`，引用元数据归原生 Store，不另建 `data/generated/` 或 thread 暂存入口。

### 安装客户端的同机体验

发行入口 `assistant-agent`（`assistant_agent.client`）使用统一 graph 常量连接业务图；必须新建会话，不能继续旧 native/Voice 保存的 thread。它通过原生 SDK 管理对话、审批和取消，
同时启动本地执行连接，不创建 Agent loop 或第二个 Agent Server。`--cwd` 选择用户目录，客户端在该目录的 `.agent/`
保存按服务地址和身份区分的会话 ID；下次启动读取服务端同一 thread，checkpoint/Memory 仍由既有服务保存。

现有 custom app 提供 `/local-devices` 注册、`/local-devices/connect` 反向 WebSocket 和注销入口。
当前只允许回环连接且拒绝带 Origin 的请求，沿用受信本机开发身份，不能当作公网帐号系统。
注册把 identity、随机 device_id 与 cwd 绑定在当前进程，配对 token 只在客户端内存及连接 header 中使用。
重载后客户端需重新启动并注册。公开 context 的可选 `device_id` 选择该绑定；run auth 校验 owner/cwd/在线状态。

默认 composition 的 `WorkingDirectoryBackend` 已支持设备分发：有 device_id 时文件、Shell、Git 和 Playwright 经客户端执行，
无 device_id 的 Studio/媒体调用保留既有本机路径。主 Agent、worker、coder 和 browser-operator 在 Tool handler 前
检查绑定设备在线；断连不会改选服务端文件 backend，已开始的操作仍可能有结果未知或不可回滚的副作用。

执行边界已补齐：cwd Skills、项目指令和压缩历史来自绑定客户端；图片生成 Provider 仍在服务端运行，
生成物分块传到客户端 outputs，上传的本地媒体先从客户端取回临时处理。浏览器使用客户端独立 Chromium。
注册仍是**同机生产接线**；正式帐号认证、TLS、设备持久注册和跨平台安装包未完成，不能宣称“另一台电脑下载安装即用”。
无设备 context 只接受服务端 Home 内路径；设备 cwd 使用注册绑定的绝对路径，不按服务端 Home 校验。
安装客户端在本机校验自己的 Home 边界，保持真实绝对路径语义，不使用实验的虚拟根模式。
客户端默认逐次 HITL；Shell 还需 `--allow-shell`，使用客户端 PATH（优先安装环境）和最小环境白名单，
拥有当前 OS 用户权限而非沙箱。完整启动与安装命令见 `scripts/README.md`。

日常开发遵循 [官方本地开发指南](https://docs.langchain.com/langsmith/local-dev-testing)，使用 `langgraph dev`。
PyCharm 的 **Agent Server (Real)** 在 V2 通过 `scripts/run_server.py --port 8088 --env-file .env` 调用官方 CLI，
直接使用本机 `hello_agent` 环境和源码，默认自动重载；`--no-reload` 可关闭。Real 指 Provider 配置，不表示生产部署。
wrapper 仅负责 env/config 来源、工作目录级单实例锁、严格端口检查和仓库外日志，不修改官方 watcher 或 Runtime。
本机只运行当前目录的 Agent Server（8088），前台 Gateway 的网页和媒体共用此后台。
冗余 `assistant-agent-cqy.service`（8089）停止并禁用自启；旧源码 worktree 保留，不作为运行实例。
统一个人工作目录是 `~/assistant_cqy/`，名称不表示 cqy 分支归属。具体启停见 `scripts/README.md`。
使用官方 `langgraph-runtime-inmem==0.33.0`；删除原本地 fork，队列调度和恢复行为跟随上游实现。
checkpoint/Store 保存在 `.langgraph_api/`，重载会重建服务进程并短暂断开连接，不代表运行中任务无中断更新。
`langgraph.json` 保留项目依赖入口、Graph、auth、HTTP 和 Store 配置；依赖安装在本机环境，变更后重新安装并重启，
不需要构建 Agent 镜像。当前 Mem0 后端不使用 Server Store embedding；旧 LangMem 部署配置已删除，
具体后端规则见 Memory authority。
不再提供项目自定义 PostgreSQL/Docker/watch 部署入口；旧 PostgreSQL 数据卷保留但不由 dev 使用，也不自动迁移。
真实 `.env` 保持未跟踪。未来需要部署验证时再按官方 `langgraph up` 流程独立配置，不混入日常开发入口。

## 新电脑安装与验收

Windows 新电脑使用 WSL2 Ubuntu 24.04，Git clone 后在虚拟环境内执行 `pip install -r requirements.txt`。
下载 BGE、导出 SigLIP2，再手动复制旧电脑 `.env`，核对模型路径和本机服务地址。
启动、目录迁移与模型准备的脚本入口见 [脚本说明](../scripts/README.md)。

```bash
python scripts/setup_local.py check
python scripts/check_documentation_authority.py --repo-root .
```

第一条只检查 mock 配置、业务、worker、Memory 三张注册图装配，不启动 Server，不代表实际对话质量验证。
