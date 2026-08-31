# LangGraph-native 可观测性

最后更新：2026-08-31

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Graph 原生 tracing 与脱敏边界的当前权威 |
| Owns | LangSmith native tracing、callback/parent 传播、thread 关联与脱敏边界 |
| Does not own | Graph 路由、Agent Server/media wire、历史 trace store/query、Provider 语义、评测 Dataset 与发布决策 |
| 源码与 schema 入口 | `native_agent/assistant_agent.py`、`agent_server/services.py`、`media/vision/observability.py`、`observability/langsmith_*.py`、`observability/trace_content_policy.py` |
| 验证入口 | `docs/authority.toml` 中 `runtime-observability.verification`；核心不变量 `OBS-001` |
| 相邻 authority | [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)、[`visual-perception-architecture.md`](visual-perception-architecture.md)、[`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md)、[`../evals/README.md`](../evals/README.md) |

## 生产 tracing

`assistant-native-v4` 直接依赖 LangChain/LangGraph callback。Agent Server、主图、子图、node、LLM 与 Tool 的
实际执行树由 LangSmith native tracing 观察；生产 composition 不创建 `ProductEventProjector`、canonical
run tree、JSONL lifecycle shadow tree 或 OTel 重建层。

Agent Server 的原生 LangChain/LangGraph callback tracing 由标准 `LANGSMITH_TRACING=true`、
`LANGSMITH_API_KEY` 与 `LANGSMITH_PROJECT` 控制。`ASSISTANT_AGENT_LANGSMITH_ENABLED` 只控制项目自有的
显式 LangSmith helper/client 路径，不能替代原生 tracing 开关。未显式启用时，mock pytest 不得创建远端
client 或发出网络请求。Provider 原始 payload、Authorization、Memory 正文和媒体正文不得进入 metadata；
Tool artifact 和 message content 是否记录遵循 LangSmith/部署脱敏配置。

### 统一 Agent 与视觉 trace 定位

统一主图不再包含模式 conditional edge。一次 AssistantAgent run 是一个 LangSmith trace；其中的 node、LLM、
Tool 各自有 child `run_id`。后台视觉线程不属于 Graph node，因此每个已关闭关键帧窗口另建一个独立
`vision.observation` root run，并显式使用 `parent="ignore"`，其 `run_id` 与 AssistantAgent run 不同；二者只以
相同 `metadata.thread_id` 关联。窗口中的真实 VLM 调用是该 root 下独立 `run_id` 的 `vlm.infer` child generation。

`vision.observation` root 原生携带 window role、window/起止/目标 sequence、semantic threshold、隔离连接标记，
inputs 携带按序 sequence/timestamp；attachments 携带按序 JPEG 和 `selected-keyframes.mp4`。附件是本次已关闭窗口
的短视频，不是持续更新的直播流。单帧不建立 run，避免 trace 爆炸。未启用标准 LangSmith tracing 时整条观测
fail-open，不创建自研 `TraceStore` 或本地 shadow trace，也不影响 VLM 业务调用。视觉 reminder 的
created/matched/delivery/cleared 生命周期同样使用带 `thread_id` 的 content-free native root events；不上传
target、message 或 embedding。

视觉 span 的父子关系、安全字段和目标帧 barrier 语义由视觉 authority 定义；本 authority 只要求它们继续
使用 LangSmith native tracing、遵守全局脱敏边界，并能通过 `thread_id` 从 AssistantAgent roots 定位到真正的
`vision.observation -> vlm.infer`。具体 SDK 快速查询步骤只在
[`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md) 维护，避免两份命令漂移。

## 旧本地观测边界

历史 canonical reader、ledger、trace query 与诊断工具不是当前主图或视觉链路的执行依赖，也不得反向决定
graph route、resume、cancel 或 terminal。新视觉观测不再写自研 `TraceStore`/日志投影事件、解析日志或启动
本地报告 UI。
历史 trace 诊断按
[`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md) 的独立 owner 执行。

未来行为评测可以把 production graph 作为 target，但 Dataset、Experiment 与 Score 的 owner 仍是
`evals/README.md`。当前旧 Release Review runner 已删除；不得从评测或本地 ledger 构造第二棵“看起来像”
生产执行树。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_observability_contract.py
```
