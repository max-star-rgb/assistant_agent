# LangGraph-native 可观测性

最后更新：2026-09-03

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Graph 原生 tracing 与脱敏边界的当前权威 |
| Owns | LangSmith native tracing、callback/parent 传播、thread 关联与脱敏边界、原生 Graph evaluation target 及当前评测边界 |
| Does not own | Graph 路由、Agent Server/media wire、Provider 语义、评测 Dataset 与发布决策 |
| 源码与 schema 入口 | `native_agent/assistant_agent.py`、`agent_server/services.py`、`providers/realtime_trace.py`、`media/vision/observability.py`、`evaluation/native_graph_target.py` |
| 验证入口 | `docs/authority.toml` 中 `runtime-observability.verification`；evaluation target 使用 `python -m compileall -q src/assistant_agent/evaluation` |
| 相邻 authority | [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)、[`visual-perception-architecture.md`](visual-perception-architecture.md)、[`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md) |

## 生产 tracing

`assistant-business-v2` 直接依赖 LangChain/LangGraph callback。Agent Server、主图、子图、node、LLM 与 Tool 的
实际执行树由 LangSmith native tracing 观察；生产 composition 不创建 `ProductEventProjector`、canonical
run tree、JSONL lifecycle shadow tree 或 OTel 重建层。

Graph root run 的 Outputs 就是生产 Graph 公开返回值：完整标准 `messages` 以及存在时的原生
`todos/structured_response`。`memory_context/status`、验证状态、Provider profile 和异步任务只留在内部
state/checkpoint，不是 root Outputs。LLM、Tool 和 single-step 信息仍由原生 child runs 表达，不展平或复制到 Outputs。

Agent Server 的原生 LangChain/LangGraph callback tracing 由标准 `LANGSMITH_TRACING=true`、
`LANGSMITH_API_KEY` 与 `LANGSMITH_PROJECT` 控制。项目不再维护第二套 LangSmith helper/client 开关。
未显式启用时，mock pytest 不得创建远端 client 或发出网络请求。Provider 原始 payload、Authorization、Memory 正文和媒体正文不得进入 metadata；
Tool artifact 和 message content 是否记录遵循 LangSmith/部署脱敏配置。

### Voice 首 token 观测

Qwen Audio Agent 前台使用上游事件驱动 Realtime Runtime，不使用 LangGraph；Gateway 是其服务宿主。
集成包的 `frontend-observation-patch.mjs` 为锁定的 2.0.1 暴露通用 `onExecution` observer，Runtime
只投影轮次、转写、模型响应、工具执行和关闭事实，不依赖 LangSmith。补丁启动前检查版本与唯一源码锚点，
可重复应用；依赖升级或锚点变化必须复核。`frontend-trace.mjs` 通过官方 JS SDK 创建
`FrontendAgent` root、`QwenRealtime` LLM 与 tool children，按上游 turn/response/call ID 关联。
模型响应结束不等于含工具交互结束；使用上游 `awaitsToolFollowUp` 判断是否等待续答。
迟到 ASR 按轮次回补 root Inputs 与已经预留该输入位置的模型快照，不追加后续消息。
每轮前台 run 使用独立 root，同一 Gateway 会话通过 `thread_id` 关联。前台工具及其即时续答结束后
本轮即结束，不等待后台任务，不按 `taskId` 合并不同轮次。进展、结果和合并播报各自形成新的前台 run，
沿用同一 Provider 会话的已观测历史；后台业务 trace 独立，通过任务 metadata 关联，不创建伪父子关系。
root 的 `Inputs.messages` 冻结为本轮首次模型调用时的 system 与完整有界消息快照；
`Outputs.messages` 保存本轮完成时的消息快照，包含此前历史、本轮工具调用、已发送的工具结果与模型回复。
迟到 ASR 只回补已经预留的位置，包括 root Inputs/Outputs 和模型 Inputs；后续轮次不改写旧快照。
首次模型调用之前 root 暂存当前历史；发生中断或关闭时仍保存已有消息。
后台通知、进展、权限及其他 context-only 文本从上游实际接受的 `conversation.item.create` 采集，
保留真实 role 和 item ID，去重后按发送顺序进入历史，不从任务存储重建或虚构用户输入。
该观测事件不记录音频、图片，不改变 Provider 会话内容，不重放消息；发送失败的内容不进入历史。
LLM Inputs 的 `messages` 包含有效 system instructions 与当前 Provider 会话的跨轮文本观测历史；
`session_configuration/response_configuration` 保存实际发送的配置及响应覆盖项，
`extra.invocation_params` 保存 tools schema、tool_choice 等调用参数。配置只在 WS 发送路径接受消息后采集，
显式响应按原生 request 关联快照，自动响应取可观测 session 配置；这不声称 Provider 已确认每次配置更新。
配置投影有 1 MiB 上限，超限省略并标记；`LANGSMITH_HIDE_INPUTS` 同时隐藏配置及 invocation parameters。
模型 Outputs 保留标准 `tool_calls`；后续模型 Inputs 连接此前各轮用户转写、模型消息与已经被 WS 发送路径接受的
`function_call_output`，按 `call_id` 关联。工具节点仍记录执行结果，模型输入使用发送时的结果投影，
不以工具原始返回值代替。发送结果最多保留 64 项、每项 16,000 字符，超限内容显式省略，重连清空；
只用于观测，不回灌模型。`input_scope=observed_session_history` 和 Inputs 中的
`observation.representation=observed_text_history` 明确其为可读文本历史，而非完整模型内部上下文。
每次响应创建独立快照，后续消息不改变旧快照；仅迟到 ASR 可以回补关联的预留位置，并标记 `asr_backfilled`。
历史最多保留 256 条消息、128 KiB 序列化字符，超限按消息组淘汰最早内容并标记 `history_trimmed`。
首次 `session.created` 接管握手前已收到的输入与轮次，补齐 `provider_session_id`，不重建 root；
重复的同 ID 握手不改变历史。已绑定 Provider 会话后收到不同 ID 的 `session.created`，或连接丢失后，
清空观测历史；旧 trace 保留。
已确认的 item 删除/截断只改变未来快照；没有文本与音频对齐时保守移除对应消息组，不推断用户实际听到的文本。
输入/输出隐藏开关均阻止工具结果或后台通知通过历史快照泄露；隐藏 Inputs 时 Outputs 不复制用户历史或 system。
run 创建后，运行中的输入更新只维护内存快照，不发送非终结 PATCH，避免依赖 ingestion 对空结束时间的解释。
终结 PATCH 一并提交冻结 Inputs、完整 Outputs 和真实结束时间；迟到 ASR 更新已结束 run 时保留原结束时间。
因此运行中的 root Inputs 可能仍是创建时快照，完整上下文在终结后可见，模型节点创建时仍携带其输入快照。
实际上下文由 Realtime 服务侧维护，LangSmith 文本投影不用于会话恢复、模型回灌或精确重放；标记
`conversation_history=provider_managed`。当前 Gateway 禁止把本地 UI/Memory 记录经 `recentMessages` 回灌，
因此重连建立新的模型上下文，界面旧记录不代表新连接的模型记忆。Outputs 为最终转写或实际增量。
LLM start_time 是首个可观测响应事件，标记
`ttft_origin=unavailable`，不据此上报 TTFT；usage 只记录实际返回的计数。
打断、失败、连接丢失和会话关闭终结未完成 span，不取消业务任务。前台 root 的 `qwen_namespace`
与 tool 的 `qwen_task_id` 关联业务 thread，不伪造前后台父子关系；前台 `thread_id/voice_session_id`
表示 Gateway 会话，不是 Graph thread。

Gateway 的直接百炼搜索由 `FrontendAgent` 下的 tool child 记录完整执行生命周期。
Python `/qwen-audio/search` 仅在该次 `BaseTool.ainvoke` 周围使用 `tracing_context(enabled=False)`，
避免跨 HTTP 缺少 parent 时再次自动创建独立 Tool root；不全局关闭追踪，不影响业务 Graph 中的 MCP children，
也不影响旧浏览器 Voice 通过 `tracing_context(parent=tool_trace)` 传播的原生父子关系。

记忆提取调度仅由前台 Realtime 会话关闭触发，业务图不再注册 Memory after_agent。
`VoiceTasks.refresh_memory` 在三次重试的提交调用周围使用 `tracing_context(enabled=False)`，
避免 HTTP 转写桥接或后台协程生成独立 `refresh_memory_extraction` root。
该局部开关不影响前台轮次、业务 Graph 或实际 `assistant-memory-v1` 后台提取的原生 tracing；
提交失败仍通过已有 memory.error/桥接错误报告。

Gateway 显式读取 `LANGSMITH_TRACING/API_KEY/ENDPOINT/PROJECT/HIDE_INPUTS/HIDE_OUTPUTS`，配置仅传给适配层；
mock 始终不创建远端 client。适配层异步排队、有界保留内容，过滤媒体和敏感字段，隐藏开关在入队前生效。
上报失败只记录固定错误码；关闭时限时排空，超时停止剩余上报。追踪为 best-effort，不阻塞语音。
已通过离线上游 Runtime 和本机 HTTP SDK fixture 验证。2026-10-16 已启用本机 Gateway 观测并重启，
通过现有 WebUI 文字输入触发真实 Qwen Realtime 响应，在 LangSmith 确认完成的
`FrontendAgent → QwenRealtime` 父子 trace、无错误与实际 token 用量。此验证未覆盖麦克风采集、
真实工具调用、语音打断或延迟验收，不等同于完整语音验收。
同日补充真实配置验收：LLM trace 的 Inputs 包含 system/user、session 配置，invocation parameters
含 5 个实际工具 schema，`conversation_history=provider_managed`；响应无覆盖时记录空覆盖对象。
本接入不复用下述旧浏览器 Voice 首 token RunTree。业务 thread metadata 的 `qwen_task_id`、`qwen_namespace` 与 run metadata
的 `qwen_submission_id` 用于任务关联和不确定提交对账，不进入模型输入。Node logger 丢弃任意上游错误正文与媒体；
上游会话/任务持久状态位于仓库外专属目录，不作为可提交日志。不能将此入口的离线 Gateway 测试报告为真实语音评测。

浏览器原生持续语音不创建 Voice Graph run。`providers/realtime_trace.py` 用官方 LangSmith `RunTree`
记录实际 WS 事件生命周期，不模拟 Graph，也不记录原始 PCM 或完整 Provider payload。
每轮根节点 `VoiceAgent` 的 Inputs 是 Provider `conversation.item.input_audio_transcription.completed` 返回的 ASR 文本；
子节点 `QwenRealtime` 记录实际 response，工具提交/查询以 tool child 记录参数和结果，工具后继续回复属于同一根 trace。
`QwenRealtime.inputs.messages` 是创建该响应时的原生会话文本快照：会话 instructions、历次 ASR、
模型回复/工具调用以及已经成功发送的工具结果或通知。根 Inputs 仍仅表示当轮新输入。
文本按原生 item_id 关联、去重，ASR 迟到只回补包含该 item 的快照，不把之后的消息塞进旧响应。
不从音频重新识别、不保存 base64/PCM、不把这份观测历史发回模型。
metadata.context_representation=observed_text 明确其为可读文本投影，而非 Provider 内部多模态/token 上下文。
本地最多保留 256 个历史条目，发生裁剪时标记 history_trimmed；模型侧未显式上报的历史裁剪不能推断。
收到原生 item 删除/截断事件后更新未来快照；音频截断缺少文字对齐时移除对应文字，不伪造“已听到”的前缀。
Outputs 优先使用 `response.done` 中的文本或 `response.audio_transcript.done` 的完整回复转写，缺失时保留实际增量。
ASR 迟到按 item_id 回补原轮次，不阻塞工具执行。语音打断标记 interrupted，挂断终结未完成 span，二者不取消业务任务。
这些文本是模型生成/识别结果，不代表用户已完整听到；首文本增量记录真实接收时间的 new_token 事件，
不以最终聚合或音频播放时间冒充 TTFT。浏览器首音频/播放延迟尚未接入。
Realtime LLM span 的 start_time 来自实际生成触发：显式 response.create 在持有发送锁、发送前记录；
VAD 自动生成以接收到 input_audio_buffer.committed 为可观测起点，metadata.ttft_origin 区分口径。
首个非空文本或工具参数 delta 记录 new_token；response.created 只关联响应，不作为 TTFT 起点。
无法关联触发时标记 ttft_origin=unavailable，并且不上报虚假的首 token 延迟。
直接搜索 span 返回搜索结果，业务委派 span 才返回 task_id/run_id/accepted。

Voice trace 的 metadata.thread_id 是语音 session UUID（不对应 Graph thread）；业务使用另一稳定 thread，各委派为独立原生业务 trace，
Inputs 只有任务 HumanMessage，随后是业务自身 loop。双方以 metadata.voice_session_id 和工具返回的 task_id/run_id 关联，
不建立业务 root 到语音 tool 的父子关系。新委派通过 Agent Server 原生 rollback 替换冲突 run；middleware 仍保留为内部细节。
后台任务结果触发的回复用实际外部通知作为输入，不伪造新的 ASR 文本。
RunTree 只有标准 tracing 开关启用时才构造和上报，追踪失败不影响语音连接。转写、工具参数/结果记录于 inputs/outputs，
遵循部署的 LangSmith 隐藏/脱敏设置，不写入诊断日志或 metadata。

### 统一 Agent 与视觉 trace 定位

一次 Studio 业务 run 是一个 LangSmith trace，worker 子图沿原生 config 传播。独立视觉处理使用
`vision.observation` root run，并显式使用 `parent="ignore"`，其 `run_id` 与业务 run 不同；二者只以
相同 `metadata.thread_id` 关联。Qwen 的 `vlm.infer` child 由 multimodal `BaseChatModel` 的 LangChain callback
原生创建；非 Qwen/manual fallback 只在自身没有原生 model run 时创建一个 generation，不得形成重复 model run。

`vision.observation` root 原生携带 window role、window/起止/目标 sequence、semantic threshold、隔离连接标记，
inputs 携带按序 sequence/timestamp。Qwen child 的 `inputs.messages` 按序展示 JPEG 与末尾最终 prompt；root 不重复
上传这些 JPEG，只保留 MIME 为 `video/mp4` 的 `selected-keyframes-video`。child 默认 output content 是
`summary`，完整结构化结果位于 details。LangSmith attachment name 不允许包含 `.`，文件类型只由 MIME 表达。
附件是本次已关闭窗口的短视频，不是持续更新的直播流。单帧不建立 run，避免 trace 爆炸。未启用标准 LangSmith tracing 时整条观测
fail-open，不创建自研 `TraceStore` 或本地 shadow trace，也不影响 VLM 业务调用。视觉 reminder 的
created/matched/delivery/cleared 生命周期同样使用带 `thread_id` 的 content-free native root events；不上传
target、message 或 embedding。

诊断时以 `vlm.infer.run_id` 为 model run 身份；领域记录的 `source_vlm_span_id` 保存该同一 ID，并与
`source_vision_trace_id/source_vision_run_id` 一起回链 root。Provider 与 model 位于 child response metadata，token
用量位于 child usage metadata；summary 与完整结构分别查看 output content 和 details。不得用 root 聚合 usage 与
child usage 相加。

视觉 span 的父子关系、安全字段和目标帧 barrier 语义由视觉 authority 定义；本 authority 只要求它们继续
使用 LangSmith native tracing、遵守全局脱敏边界，并能通过 `thread_id` 从业务 Graph 或原生 Voice roots 定位到真正的
`vision.observation -> vlm.infer`。具体 SDK 快速查询步骤只在
[`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md) 维护，避免两份命令漂移。

## 本地兼容层已退休

历史 delivery/latency/conversation/metrics/persistence/ledger/visual-content 投影和未接线的项目自有 LangSmith wrapper
已经删除。旧 `TraceStore`、run/trace summary query 与 turn summary 没有生产消费者；visual eval 也只读取领域
记录中的原生 LangSmith trace link，因此这些兼容模块已经退休。新视觉观测不写本地 shadow trace、解析日志或启动
本地报告 UI。
历史 trace 诊断按
[`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md) 的独立 owner 执行。

## 原生 Graph evaluation target

`evaluation/native_graph_target.py` 保留 `NativeGraphEvaluationTarget`，通过
`AgentServerExecutionOwner.compose()` 直接调用生产 Graph，返回公开输出、thread ID 与 run ID，
不创建第二套 Runtime。当前测试清理已删除 system eval 数据和 runner 实现；残留脚本不代表可运行的能力评审。
当前没有 Dataset、Experiment、Score 或 Release Review runner 的执行契约，后续重建必须显式补齐。
不得从评测或本地 ledger 构造第二棵“看起来像”生产执行树。

默认验证仅使用 mock/local/offline，并关闭远端 tracing。真实 Provider 评审必须由用户明确要求，
显式设置 `MULTIMODAL_AGENT_PROVIDER_MODE=real`、本机私有配置以及评审所需的 operator 确认开关；
不能因为发现 key 自动开启，也不能用 mock fallback 伪装真实结果。报告必须说明调用范围与验证结果。

## 验证

```bash
python -m compileall -q src/assistant_agent/evaluation src/assistant_agent/agent_server/services.py src/assistant_agent/media/vision/observability.py
```

以上仅检查语法；行为变更的临时测试按 `tests/README.md` 显式运行。
