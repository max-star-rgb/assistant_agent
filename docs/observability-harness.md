# LangGraph-native 可观测性

最后更新：2026-08-28

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Graph 原生 tracing 与脱敏边界的当前权威 |
| Owns | LangSmith native tracing、callback 传播与脱敏边界 |
| Does not own | Graph 路由、Agent Server/media wire、历史 trace store/query、Provider 语义、评测 Dataset 与发布决策 |
| 源码与 schema 入口 | `native_agent/assistant_agent.py`、`agent_server/services.py`、`observability/langsmith_*.py`、`observability/trace_content_policy.py` |
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

统一主图不再包含模式 conditional edge。诊断视觉调用时应定位 `vision.observation -> vlm.infer` generation，
不要把 `AssistantAgent` 的 Todo、task 或 Tool span 当作 VLM generation。

视觉 span 的父子关系、安全字段和目标帧 barrier 语义由视觉 authority 定义；本 authority 只要求它们继续
使用 LangSmith native tracing、遵守全局脱敏边界，并能从统一 Agent trace 导航到真正的 `vlm.infer` generation。

## 旧本地观测边界

历史 canonical reader、ledger、trace query 与诊断工具不是当前主图的执行依赖，也不得反向决定 graph
route、resume、cancel 或 terminal。历史 trace 诊断按
[`observability-diagnosis-runbook.md`](observability-diagnosis-runbook.md) 的独立 owner 执行。

未来行为评测可以把 production graph 作为 target，但 Dataset、Experiment 与 Score 的 owner 仍是
`evals/README.md`。当前旧 Release Review runner 已删除；不得从评测或本地 ledger 构造第二棵“看起来像”
生产执行树。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_observability_contract.py
```
