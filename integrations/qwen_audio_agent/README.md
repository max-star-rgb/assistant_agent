# Qwen Audio Agent 实验接入

文本记忆直接使用 qwen-audio-agent 2.0.1 原生 Markdown 模块和 `memory` 工具，不再经过 Mem0 或 Python Memory Graph。
文件存放在 `QWEN_AUDIO_STATE_DIR/memory/<SHA-256(ASSISTANT_AGENT_USER)>/USER.md` 和 `MEMORY.md`，支持原生读写、版本校验和上下文订阅。
旧 Mem0 数据保持原样，不自动迁移。Studio/SDK 的旧文本 backend 当前设为 disabled，不共享 Node 文档。
Gateway 初始化时先召回远端视觉快照，之后每成功处理完成 10 个视频段刷新一次并注入前台提示词；默认每段 30 秒，复用 Python 视频归档窄服务，不经过业务 Agent，不提供模型召回 Tool，也不写 Markdown。前台每 2 秒读取本地缓存，刷新失败保留上一快照。

启动前台会由同一 Node 进程同时监听 `0.0.0.0:3101`（WebUI 与媒体）和 `0.0.0.0:8081`（媒体）。
媒体服务可直接连接 `ws://<宿主局域网IP>:3101/agent-service/v1?number=00860332`，自动初始化会话，无需控制握手。
PyCharm 选择 **Realtime Server** 即启动两个端口；停止前台同时释放。`Assistant (All)` 仍启动前后台两进程。
8081 仅开放 `/agent-service/v1`，不暴露 WebUI/管理接口；任一端口占用会令前台启动失败并清理已开的监听。
旧 Python 转发进程已移除。媒体 `VIDEO` 的 H.264 解码与可选后台视觉流水线通过本机内部通道复用，音频不经过 8088。

V2 媒体音频接入：`8081/agent-service/v1?number=<号码>` 自动初始化后，复用本 Gateway 的公开 GCP 7
`/api/realtime` 会话。输入为 16kHz PCM16，模型音频按 24kHz PCM16 回传媒体，由媒体编码为 WebRTC。
文本字幕禁止重复 TTS，必须回报真实播放状态。完整合同见
[`media-agent-service-websocket.md` 第 5.1 节](../../docs/media-agent-service-websocket.md#51-v2-双向音频-audiostreamv1)。
本机 personal 身份固定为 `local-developer`，当前不支持多个媒体用户的身份隔离；网页与媒体可同时运行独立会话，共享该用户的长期记忆。
`frontend-concurrency-patch.mjs` 对锁定的 2.0.1 将连接与语音租约限定为 owner + session；同一会话仍保留重复连接与显式接管规则。
每条媒体连接使用服务端随机生成的 Gateway session ID，音频、上下文、任务通知与关闭提取相互隔离；自动通知不领取其他会话的结果。
机器麦克风被宿主独占时的全局输入暂停仍保留。该补丁检查版本和源码锚点，依赖升级必须复核。

独立 Node Gateway + 既有 Agent Server 的薄适配。此入口尚未替换 `/media-lab`，真实语音、原生 Server 联调和 WebUI 逐次审批尚未验收；不能据离线通过宣称 M1/M2 完成。

## 已实现与边界

- 锁定 `qwen-audio-agent@2.0.1`，通过公开 `backend-adapter-sdk`、`gateway-application` 组合。启动时应用版本受限的 WebUI 视觉状态文案补丁及前台通用 observer 补丁。补丁可重复执行，重装依赖后自动应用；版本或目标变化时明确失败，升级时必须复核。观测补丁不改变模型、工具与任务执行决策。
- 一个 Gateway task 对应一个 `assistant-business-v2` thread，不 rollback 其他任务；Gateway 自身仍控制排队与并发。
- 业务只接收规范化委派文字，固定关闭业务 Memory 和原生 HITL。暂不接收附件，明确返回 unsupported，不静默丢弃。
- 查询、取消和原生输入恢复；失败、提交未知、观测超时分别报错，不重投不确定的副作用请求。
- Gateway 挂断/关闭只停止观测，原生业务继续。重启后可以用任务查询命令对账，不保证自动恢复旧语音上下文或自动播报。
- 网页文字和语音使用原生 ConversationSync；会话结束由 Node 原生 extractor 按至少四条新用户记录提取 Markdown，不设时间冷却；同一批记录仍只消费一次。未达到门槛时不自动提取，用户可用 memory 工具明确保存。推测偏好学习、摘要、定时提醒、知识库、直接联网和 MCP/OpenAPI 保持关闭。
- 日志只输出事件名、任务标识、状态和耗时，不输出上游错误正文、完整任务或媒体；上游自身的任务/会话状态保存在仓库外数据目录，按其保留策略管理。

**授权：** 沿用官方前台 runtime；明确请求直接执行，需确认时由前台交流，不增加统一的提交前审批。后台不再逐工具暂停，不提供自定义审批工具或授权缓存。官方权限工具按真实 capability 暴露；本 adapter 不发布授权请求。旧 HITL 中断不会自动批准，需在 Studio 单独处理。其他入口的审批配置保持原样。

发布版任务/会话授权工具被禁用，WebUI 不再渲染授权按钮，旧 Adapter 授权入口提示使用语言审批工具。启动应用版本受限的审批兼容补丁并更新静态资源版本；重装依赖后自动重施，升级需复核。超大操作详情不会截断后放行，改由 Studio 审核；未知提交结果不自动重试。Studio 仍可恢复原生 thread，但不要与语音同时审批同一任务（客户端检查与 resume 之间没有跨客户端原子锁）。重启 Gateway 后旧审批请在 Studio 核对。真实语音识别与模型是否准确判断同意仍需真人验收，离线测试不宣称完整 M1/M2 通过。

**结果交付：** 桥接层从成功 ToolMessage 的显式交付声明提取当前 thread 的图片，检查可读性后返回 Gateway artifacts。执行完成与交付失败分别报告；交付失败时不自动重新生成，不提供产物重试工具。前台禁用 `get_current_time`，日期由运行上下文提供。当前图片地址使用本机 Agent Server，仅支持同机浏览器。生成工具的运行异常终止业务；生成成功后的本地复制/设备上传失败保留服务端产物。

原生补充输入只支持单个 interrupt 的值为 `{"type":"input","mode":"text","prompt":"..."}`；回应必须为非空 `text`。不把普通最终回答中的问句识别为 interrupt；未知 interrupt 留待 Studio 处理。本接入没有向业务图添加新的提问工具。

## 环境与离线检查

**取消限制：** 仅对仍在执行的 run 提交取消。已终结的 run 返回 `business_already_finished_query_Server_result`；存在 checkpoint interrupt 时返回 `interrupt_requires_Studio_resolution`，请在 Studio 处理。上游 TaskManager 会把取消拒绝显示为任务失败并停止前台观察，因此该显示不代表原生业务失败；请查询原生结果。此兼容问题尚未解决，不作为完整生命周期验收通过。

需要 Node `^22.22.2 || ^24.15.0 || >=26.0.0`、npm 10+。本机系统 Node 18/npm 9 不满足要求。可以使用本机已有 Node：

```bash
export PATH=/home/lenovo1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:$PATH
cd /home/lenovo1/pycharm_project/assistant_agent/integrations/qwen_audio_agent
npx --yes npm@10.9.4 ci --ignore-scripts --no-audit --no-fund
node --test --test-concurrency=1 test/*.test.mjs
```

`--ignore-scripts` 适用于本接入使用的云端 WebSocket/WebUI 能力；本接入不启用依赖中的本地语音或桌面原生模块。npm 下载与测试不调用模型。Node 测试使用临时本机 HTTP fixture，真实 Gateway 仅绑定临时端口，不连接现有后台实例。测试串行运行，因为版本受限补丁测试与 Gateway 启动会修改同一份上游安装文件。

Python 桥接测试：

```bash
cd /home/lenovo1/pycharm_project/assistant_agent
MULTIMODAL_AGENT_PROVIDER_MODE=mock LANGSMITH_TRACING=false /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/qwen-audio-agent
```

该 pytest 目录是项目约定的可删除临时测试。Node 包内测试含官方 BackendPort 一致性套件、真实上游 TaskManager 到 HTTP fixture 的委派路径，以及真实 conversation record 回调到 Memory 桥的路径。HTTP fixture 验证适配协议，不代替真实 Agent Server/模型验收。

## 真实验收启动

2026-10-15 本机试用配置：已在用户授权下启用真实 Provider，完成官方 WebUI 文本对话和原生业务委派连通性检查，并在 `assistant-agent-runtime` 查询到成功的业务 trace。2026-10-16 前台 LangSmith 已启用，完成真实 Realtime 文字交互及远端父子 trace 验证。麦克风采集、真实打断与完整 HITL 尚未验收。

本机 `assistant-agent-v2.service`、`assistant-qwen-gateway.service` 已配置为 systemd 用户服务，登录后自动启动。Gateway 主 unit 仅依赖 `assistant-agent-v2.service`，部署 drop-in 将目标覆盖为 V2 的 `http://127.0.0.1:8088`，旧 `assistant-agent-server.service` 已停止；冗余 8089 实例已停用。不能用 drop-in 的空 `Requires=` 清除主 unit 的旧依赖，须核对 systemd 合并后的依赖，避免旧服务重启连带中断 Gateway。直接打开 `http://127.0.0.1:3101/`；首次语音使用需允许麦克风。维护时使用 `systemctl --user restart assistant-qwen-gateway`；停止整套试用使用 `systemctl --user stop assistant-qwen-gateway assistant-agent-v2`。不要同时从 PyCharm 启动同一目录的服务。

仅在明确授权真实测试后填写未跟踪 `.env`。先确认目标 V2 Agent Server 位于 `8088`，手动启动时显式设置 `ASSISTANT_AGENT_URL=http://127.0.0.1:8088`，并按项目规则配置其 Provider 模式；本包不会启动、重启或自动切换 Python Server。

```bash
cp .env.example .env
# 手动配置 key，并显式设置：
# MULTIMODAL_AGENT_PROVIDER_MODE=real
# QWEN_AUDIO_ENABLE_REAL=1
node --env-file=.env src/gateway.mjs
```

打开 `http://127.0.0.1:3101`，使用随发布包提供的官方 WebUI。默认 mock 会明确拒绝启动 live Gateway，key 已存在也不会自动启用。指定模型不可用时保留失败，不切换其他模型。

日常使用固定打开 `3101`，无需输入 V2 的服务端口。Gateway 的代码默认值、模板和本机配置均指向
`http://127.0.0.1:8088`；8089 已停用。页面显示“待命”时可直接输入文字发送；语音点击一次
“开启麦克风”，首次使用需浏览器授权。页面加载不会自动开始采集麦克风。
`/media-lab` 暂留作诊断与回退；在 Gateway 的真实通话、打断、业务委派及审批边界完成验收前不删除。

Gateway 使用独立配置/数据目录，忽略继承的其他 Provider、代理配置；选定的 LangSmith 配置仅传给观测适配层。此启动器是独立进程入口，不应在同一 Node 进程中嵌入多个 Gateway。按本机部署要求监听所有 IPv4 网卡并启用原生 LAN 模式；原生 `/api/realtime` 的远端认证与来源检查保留。媒体 `/agent-service/v1` 使用既有个人媒体协议，不启用 Tailscale、WebRTC 或多用户。

## 前台执行观测

实时 Agent 使用项目 `prompts/PROMPT.md`，基于官方 2.0.1 正文仅修改记忆个性化规则；默认人设使用官方 `ASSISTANT.md`。
原生 `buildFrontendInstructions`、`resolveAssistantProfile`、`buildMemoryContext` 负责组装，主提示词通过目录配置直接加载。

| 项目文件 | 用途 |
| --- | --- |
| [`prompts/BACKEND_TASK.md`](prompts/BACKEND_TASK.md) | `spawn_thinking` 的能力、委派时机和任务内容 |
| [`prompts/TIME_LOCATION.md`](prompts/TIME_LOCATION.md) | 时间地点；当前日期按上海时区填入，地点固定 |
| 上游 `server/src/memory/PROMPT.md` | 直接使用原生记忆使用说明，经原生 memory feature 注入；不维护项目副本 |
| [`prompts/PROMPT.md`](prompts/PROMPT.md) | 主提示词；区分 `<user_preferences>` 偏好和 `<user_memory>` 事实，其余沿用官方内容 |

记忆使用说明恢复上游原文；版本受限补丁负责撤销旧项目记忆说明覆盖，并接通时间地点与视觉记忆区块。
原生 USER.md 使用 `<user_preferences>`，MEMORY.md 使用 `<user_memory>`；保持版本、空内容省略和原生编辑刷新。
主提示词按当前要求、明确长期偏好、默认人设排序，观察推断与历史事实不作为用户指令。
文本记忆通过原生 `memory` 读取或编辑。

自动提取模型由 `QWEN_AUDIO_MEMORY_MODEL`（默认 qwen-flash）、`QWEN_AUDIO_MEMORY_BASE_URL`（默认百炼兼容地址）和
`QWEN_AUDIO_MEMORY_API_KEY` 配置；未单独提供 key 时复用 DASHSCOPE_API_KEY。仅 real/operator 授权后的 Gateway 可调用。

编辑文件后，统一重启实时会话服务并重新连接以确保全部生效：
`systemctl --user restart assistant-qwen-gateway`。固定正文与默认风格在原生 builder 重建时读取，
后台工具描述和记忆规则在服务启动时读取；不提供文件监听热更新。不要修改 `node_modules` 内的默认提示词，
也不要把真实用户记忆写入这些受版本管理的文件。原生读取上限为正文 16,000 字符、风格 4,000 字符，保持简短。

后台描述只指导 LLM 委派，不扩展工具权限；后台能力边界变更时同步复核。

前台 Agent 继续使用上游 Realtime Runtime。启动时额外应用 `frontend-observation-patch.mjs`，为 2.0.1
开放通用执行 observer；版本和源码锚点变化时明确失败，重新安装依赖后自动应用。补丁不依赖 LangSmith，
不参与模型、工具和任务决策；该兼容补丁同时明确关闭本地历史回灌路径。集成层 `frontend-trace.mjs` 使用官方 SDK 上报
`FrontendAgent → QwenRealtime / tool`，业务 Graph 仍独立追踪。

在 Gateway 自己的未跟踪配置中设置 `LANGSMITH_TRACING=true`、`LANGSMITH_API_KEY`、`LANGSMITH_PROJECT`，
需要时设置 `LANGSMITH_ENDPOINT`；重启 Gateway 后生效。支持 `LANGSMITH_HIDE_INPUTS/HIDE_OUTPUTS=true`。
mock 始终禁用远端观测。语音正文/工具输入输出位于 trace 内容，不进入日志或 metadata；不上传音频。
LLM Inputs 展示实际发送的 system instructions、`session_configuration/response_configuration`，
invocation parameters 展示工具 schema 与响应参数。每次响应保存独立的跨轮文本历史快照，包含 ASR、模型回复、
工具调用和实际发送的工具结果，并标注为 `observed_text_history`；外层 Inputs 仍只有本轮请求。
迟到 ASR 只回补对应位置；新 Provider 会话清空观测历史，删除/截断只影响未来快照，超限明确标记。
真正的上下文仍由 Realtime 服务侧维护，这份只读记录不回灌模型，也不宣称精确复原模型内部上下文；
本接入关闭上游把本地 `recentMessages` 回灌模型的路径。界面和 Memory 桥仍可保留记录，
但重连后的模型上下文不从这些记录恢复。当前不计算 TTFT。schema、关联和保留边界以
[`observability-harness.md`](../../docs/observability-harness.md) 为准。

已通过离线上游 Runtime、工具续答、打断/关闭、迟到 ASR、隐藏设置及 SDK 本机 HTTP 提交验证。
2026-10-16 已配置并重启本机常驻 Gateway，通过现有 WebUI 的最小文字输入调用真实 Realtime 模型，
在 LangSmith 确认成功的 `FrontendAgent → QwenRealtime` trace 与 token 用量。
同日补充配置观测验收：远端 LLM trace 含 system/user、实际 session 配置与 5 个工具 schema，
并标记历史为 `provider_managed`；单次响应没有覆盖项时 `response_configuration` 为 `{}`。
本次未调用工具，未测试麦克风、真实语音打断或延迟；这些仍需专项验收。

默认状态目录为 `~/.local/state/assistant_agent/qwen_audio`。其中 `integration-id` 隔离不同安装下的顺序 task 编号；请与整个状态目录一起备份，不单独删除或替换。Agent Server 的数据持久性仍受其当前 in-memory 部署约束。

停止使用 Ctrl+C；这不会撤销已完成副作用，也不会取消已受理业务。需要取消时使用任务取消操作。观测超过 1 小时结束本地等待，业务可能仍在运行，应查询后处理。

## 查询与 Studio 审批

使用相同 `.env` 和状态目录，根据 WebUI 中的任务编号查询：

```bash
node --env-file=.env src/task.mjs task_1
```

命令只读取原生 thread/run，输出 `thread_id`、`run_id` 和状态，不调用 Provider。用 `thread_id` 在已有 Studio 中打开对应业务 thread，如遇迁移前遗留的 HITL，以同一 `ASSISTANT_AGENT_USER` 身份单独处理。普通语音打断不等于取消业务。

原生正常图 interrupt 的 run 也可能为 `success`；`interrupted` 还可能表示已停止，必须结合 checkpoint 判断待审批或待补充状态。查询结果为 `submission_unknown` 或提交报错时，不能自动重复执行。相同 task ID 的不同指令会被拒绝，避免错误返回旧任务结果。

## Memory 桥

Python custom routes 为 `/qwen-audio/capabilities` 和 `/qwen-audio/search`，只允许已认证的本机请求；正文不能指定身份。旧文本读写及转写提交路由已删除，文本记忆由 Node 原生模块处理。

网页文字与语音直接使用上游 `ConversationSync.pendingRecords()`，媒体输入先经协议适配进入同一原生记录路径。关闭 hook 消费本批 `role=user` 记录，不另建转写缓存；原生去重和消费游标保证重复关闭不重复提交，恢复的历史、模型回复和工具结果不进入提取。原生默认保留最近 100 条会话消息，长会话提取范围受其保留策略限制。提交上限为 128 项、单项 16,000 字、总计 64,000 字，超限明确失败。网络失败通过错误日志和 health 暴露，Python 保留三次提交重试，使用 `after_seconds=0` 立即运行 Memory Graph，不承诺 exactly-once 写入。

前台默认暴露 Agent Server 已装配的百炼 MCP 工具 `mcp_bailian_bailian_web_search`，通过现有认证薄桥复用其原始描述、参数 schema 和执行实现，不依赖 Memory 开关。百炼配置沿用业务端的 MCP allowlist 与 `QWEN_API_KEY`，见 `docs/tool-calling-architecture.md`；未配置时不暴露，也不回退到其他搜索。旧 `web_search`、`fetch_url` 已从本项目前台工具面移除，相关 Provider 不再实例化。复杂搜索仍可委派业务图，真实搜索尚未验收。浏览器摄像头与媒体视频可进入前台原生 Realtime 会话；视觉提醒与历史找物仍属于独立后台能力，本包不绕过或替换已有并行视觉流水线。

## 当前交付状态与回退

- 任务 0—3：版本锁定、Gateway 组合、Adapter 和主要生命周期已有离线证据；真实语音与原生 Server 集成尚待验收。
- 任务 4：补充输入与 Studio 恢复已离线验证；WebUI 逐次审批未完成。
- 任务 5：Memory 薄桥及前台百炼 MCP 搜索已接入并离线验证；真实后台记忆提取及直接搜索尚未验收。
- 任务 6：配置、查询和回退说明已提供；尚未切换默认入口。
- 媒体前台视频桥已接入：`/agent-service/v1` 的 `VIDEO + audioStreamV1` 将 H.264 解码为 JPEG，经同一个 Gateway 会话发送给 Realtime 模型；独立于后台视觉开关。协议、限流与上下文语义见 `docs/media-agent-service-websocket.md` 第 5.2 节；真实模型联调尚未验收。

回退时停止此 Gateway，继续使用 V2 的 `http://127.0.0.1:8088/media-lab`。不同时让两套入口持有同一个语音会话。当前实验接入不改变 Studio/SDK 的业务图和旧语音入口。

外部依据：[官方 Backend Adapter SDK](https://qwenaudio.github.io/qwen-audio-agent/reference/backend-adapter-sdk)、[发布仓库](https://github.com/QwenAudio/qwen-audio-agent)。实现以锁定发布包的实际接口为准。

升级本次改动前，停止前台服务，在本目录执行 `npm ci --ignore-scripts` 恢复干净的固定版本依赖，
再启动服务并重新连接。旧版修改过依赖包的审批代码；仅刷新浏览器不能移除这些补丁。
