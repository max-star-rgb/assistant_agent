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

Qwen Audio Agent 实验入口目前只提供集成层脱敏事件日志及原生业务 Graph tracing；它未接入下述旧浏览器 Voice
首 token RunTree，也未完成真实延迟验收。业务 thread metadata 的 `qwen_task_id`、`qwen_namespace` 与 run metadata
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
