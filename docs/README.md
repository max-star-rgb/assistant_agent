# 项目文档导航

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | `docs/authority.toml` 路由后的文档导航页 |
| Owns | 当前 authority 的人类可读主题导航、非当前材料边界 |
| Does not own | 任何业务或架构事实、测试策略、system eval 规则、源码行为 |
| 源码与 schema 入口 | `AGENTS.md`、`docs/authority.toml` |
| 验证入口 | `python scripts/check_documentation_authority.py --repo-root .` |
| 相邻 authority | 下表列出的具体 authority；测试见 `../tests/README.md`，system eval 见 `../evals/README.md` |

开始工程任务时，先由 [`authority.toml`](authority.toml) 根据 `source_globs` 和 `read_when` 选择 domain；
进入本页时再按主题导航到具体 authority。不要预读全部文档。

| 主题 | 当前 authority |
| --- | --- |
| Agent Server、thread/run/checkpoint、部署和公开入口 | [agent-server-architecture.md](agent-server-architecture.md) |
| Media-Agent WebSocket、音视频 wire contract | [media-agent-service-websocket.md](media-agent-service-websocket.md) |
| 统一 Assistant、Memory middleware、通用 worker、stream 与原生 state | [runtime-event-stream-architecture.md](runtime-event-stream-architecture.md) |
| Tool、MCP、Tool Profile、Provider-native 能力和副作用治理 | [tool-calling-architecture.md](tool-calling-architecture.md) |
| 长期 Memory Graph、冻结上下文和 backend | [memory-service-architecture.md](memory-service-architecture.md) |
| Mem0 与远端视觉 Memory Service API | [memory_server_api_spec.md](memory_server_api_spec.md) |
| Prompt、conversation、context budget 和 compaction | [context_engineering_status.md](context_engineering_status.md) |
| 实时视觉、关键帧、embedding、提醒和历史找物 | [visual-perception-architecture.md](visual-perception-architecture.md) |
| Multi-agent、delegation、A2A 和 transport | [agent-communication-routing.md](agent-communication-routing.md) |
| LangSmith tracing 与可观测契约 | [observability-harness.md](observability-harness.md) |
| LangSmith run_id/trace_id/thread_id 快速定位与机器事实诊断 | [observability-diagnosis-runbook.md](observability-diagnosis-runbook.md) |
| pytest、core invariant、临时 TDD 和验证范围 | [tests/README.md](../tests/README.md) |
| system eval、真实 Provider 门禁和 evaluation target | [evals/README.md](../evals/README.md) |

`docs/development/**`、`docs/superpowers/**` 是开发或历史材料，`docs/interview/**` 是面试资料；只有任务明确点名时才读取，均不作为当前事实权威。
