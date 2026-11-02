# LangGraph-native 可观测性

最后更新：2026-09-03

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Graph 原生 tracing 与脱敏边界的当前权威 |
| Owns | LangSmith native tracing、callback/parent 传播、thread 关联与脱敏边界、原生 Graph evaluation target 及当前评测边界 |
| Does not own | Graph 路由、Agent Server/media wire、Provider 语义、评测 Dataset 与发布决策 |
| 源码与 schema 入口 | `native_agent/assistant_agent.py`、`agent_server/services.py`、`tools/artifact_tracing.py`、`media/vision/observability.py`、`evaluation/native_graph_target.py` |
| 验证入口 | `docs/authority.toml` 中 `runtime-observability.verification`；evaluation target 使用 `python -m compileall -q src/assistant_agent/evaluation` |
| 相邻 authority | [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)、[`visual-perception-architecture.md`](visual-perception-architecture.md)、[`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md) |

## 生产 tracing

`assistant-native-v4` 直接依赖 LangChain/LangGraph callback。Agent Server、主图、子图、node、LLM 与 Tool 的
实际执行树由 LangSmith native tracing 观察；生产 composition 不创建 `ProductEventProjector`、canonical
run tree、JSONL lifecycle shadow tree 或 OTel 重建层。

Graph root run 的 Outputs 就是生产 Graph 公开返回值：完整标准 `messages` 以及存在时的原生
`todos/structured_response`。`memory_context/status`、验证状态、Provider profile 和异步任务只留在内部
state/checkpoint，不是 root Outputs。LLM、Tool 和 single-step 信息仍由原生 child runs 表达，不展平或复制到 Outputs。

Agent Server 的原生 LangChain/LangGraph callback tracing 由标准 `LANGSMITH_TRACING=true`、
`LANGSMITH_API_KEY` 与 `LANGSMITH_PROJECT` 控制。项目不再维护第二套 LangSmith helper/client 开关。
未显式启用时，mock pytest 不得创建远端 client 或发出网络请求。Provider 原始 payload、Authorization、Memory 正文和媒体正文不得进入 metadata；
Tool artifact 和 message content 是否记录遵循 LangSmith/部署脱敏配置。

### 工具产物附件

业务 Tool 继续返回标准 `content_and_artifact`。`native_content_and_artifact` 可接收受信的
`artifact_resolver(ref) -> (mime_type, bytes) | None`；统一 `tools/artifact_tracing.py` 只消费
`assistant_agent_delivery_v1.output_refs`，不解析模型正文，不按 Tool 名猜测字段。
解析后的文件以原生 `Attachment` 加到当前 `run_type=tool` 的 RunTree，由 LangChain 的
`on_tool_end` 上传到同一个 Tool run，不增加 child span，不手工创建/更新第二套 run tree。
图片可在 LangSmith 附件中查看；其他格式是否能预览由 LangSmith UI 决定，不能预览时作为附件下载。

目前图像生成/编辑已接入。图片 resolver 复用现有身份和线程归属校验；每次最多 4 个文件、每文件最多
25 MiB，空文件或非法 MIME 不上传。名称为内部引用摘要，不含路径或扩展名，类型由 MIME 表示。
解析在图像工具现有异步线程边界中执行。附件字节不写入模型 observation、ToolMessage artifact 或 checkpoint；
原有内部引用及用户交付不变。关闭 tracing、local-only tracing、未采样或禁用/过滤输出时不读取文件。

LangSmith 当前没有公开的采样结果/输出脱敏判定 API；兼容读取集中在 `artifact_tracing.py`，由临时测试核验
langsmith 0.11.1 与 langchain-core 1.6.0 的原生 ToolNode 行为。字段变化时默认不上传；配置自定义 anonymizer、
buffered run 过滤器或多项目 replicas 时也保守跳过附件，避免二进制绕过脱敏策略。
resolver 失败只记录不含媒体、路径或异常正文的警告；上传由官方 tracer 负责，失败不重试业务工具。
标准 tracing 启用后，允许展示的产物字节会上传到当前 LangSmith 项目，保存期限按项目策略；不自动回填历史 trace。
其他音视频/文档工具可复用同一 MIME/bytes 接口，但须先提供自身受信 resolver；3D 异步提交不代表成品已存在。

3D 回调使用本机结构化日志记录 `image_to_3d_callback_received`（媒体类型、URL 是否存在），
`media_artifact_dispatch`（线程及连接的摘要标识）和 `media_artifact_sender_completed`；没有订阅者时记录
`media_artifact_no_subscriber`。`chat` 使用与 WebSocket 发送日志相同的摘要，可跨回调和连接关联。
这些日志不记录媒体 URL、图片或消息正文。`sender_completed` 仅证明发送函数返回，不证明媒体端接收、解析或渲染成功。

### 统一 Agent 与视觉 trace 定位

统一主图不再包含模式 conditional edge。一次 AssistantAgent run 是一个 LangSmith trace；其中的 node、LLM、
Tool 各自有 child `run_id`。后台视觉线程不属于 Graph node，因此每个已关闭关键帧窗口另建一个独立
`vision.observation` root run，并显式使用 `parent="ignore"`，其 `run_id` 与 AssistantAgent run 不同；二者只以
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
使用 LangSmith native tracing、遵守全局脱敏边界，并能通过 `thread_id` 从 AssistantAgent roots 定位到真正的
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
