# Scripts 入口索引

这里只保留当前 runtime、观测、评测和专项验收仍在使用的入口。一次性排障或已由 pytest、
eval、Gateway 主链路覆盖的 probe 不应继续沉积到本目录。

- `scripts/check_documentation_authority.py`：离线校验 `docs/authority.toml` 的 owner、路由、排他事实与
  changed-path 复核范围；输出结构化 JSON，不读取 `.env`、不联网、不改写文档。

## Realtime runtime

- `scripts/run_server.py`: starts the FastAPI backend with Gateway, media, HTTP,
  memory, trace, and tool-governed runtime routes. 启动完成后默认打印从实际 app/runtime
  收集的精简运维摘要，包括 bind、健康检查、Provider、Tool 分类计数、Worker、已启用集成、
  安全开关，以及 Runtime completeness ledger、Langfuse export、Gateway lifecycle、Agent-Service
  delivery audit 和 Gateway text log 的分层观测位置；只有排查 Tool 装配时才使用
  `--startup-details` 展开按 plugin ownership 分组的完整清单。
  Graph memory 在 Langfuse 中只投影 `memory.recall` / `memory.commit` 的 backend、状态、数量、延迟和
  issue code，不记录 Memory 或对话正文；单条记忆演化需在受控运维边界使用后端原生 history API 钻取。
- `scripts/run_langfuse.py`: PyCharm-friendly local Langfuse supervisor. It starts
  the ignored `.data/langfuse` Compose stack, waits for health, stays attached as
  one Run process, and stops the containers without deleting data when terminated.
- `scripts/run_qdrant.py`：PyCharm-friendly 本地 Qdrant supervisor。它只启动
  `docker/mem0/compose.yaml` 的 `visual-memory` profile 和 `qdrant` service，等待
  `http://127.0.0.1:6333/healthz` 就绪，并作为一个 Run process 持续运行。仓库已提供共享配置
  `.run/Qdrant.run.xml`；在 PyCharm 选择 **Qdrant** 后点击 Run 即可启动，点击 Stop 会停止容器但保留
  `qdrant_data` volume。命令行可运行
  `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_qdrant.py`。
- `scripts/run_mem0.py`: PyCharm-friendly local Mem0 operator console. It starts
  Mem0 + Qdrant, waits for health, then stays attached at a `mem0> ` prompt with
  `help`, `status`, `list`, `get`, `history`, `add`, `update`, `delete`, `clear`,
  and `exit` commands. The console directly displays and manages raw records across
  all Mem0 identities; `add` stores text directly by default and only enables
  Mem0 inference with `add --infer`. Single-record deletion requires confirmation
  unless `--yes` is supplied. `clear` accepts one or more raw identity filters;
  `clear --all` resets every memory plus Mem0 history and always requires typing
  `DELETE ALL MEMORIES` (`--yes` is rejected for this scope). Exiting leaves both
  containers and persistent data running. 这是直接读写 Mem0 sidecar 原生记录的 operator console，
  不是 Runtime graph-native Memory 管理入口，也不经过 `memory_recall` / `memory_commit` 节点。
Runtime 后端由受信 `MEMORY_BACKEND` 配置在 composition root 中排他装配，不提供独立 Plugin CLI。
- `scripts/migrate_mem0_memories_to_chinese.py`：检查或迁移一个 runtime 用户已有的
  Mem0 记忆为简体中文。默认命令只读；更新要求 real Provider mode、已配置的 Qwen 和
  Mem0，并同时传入 `--apply` 与 `--allow-real-provider`。输出只包含数量、memory ID、
  状态和稳定错误码，不持久化记忆正文或 Provider 响应。
- `scripts/agent_cli.py`：HTTP/SSE 产品 CLI，与 Web UI 共用 `/agent/run`。默认请求
  `text/event-stream` 并逐 delta 输出；`--no-stream` 可验证 JSON 表示，交互模式支持
  `/standard`、`/deep research`、`/new`，Ctrl-C 会按 `user_id + session_id + run_id`
  请求取消当前 run。terminal annotations 只打印本轮实际引用来源的紧凑诊断，不修改正文。
- `scripts/media_simulator.py`: server-backed Media-Agent protocol simulator for
  `/agent-service/v1`; type text repeatedly, or use `/new [sessionId]` to open a
  new media session. In interactive mode, `/deep research` selects
  `assistantMode=deep_research` for subsequent turns and `/standard` switches back;
  these local commands are not sent as chat text. If the server closes the
  connection during an interactive turn (for example close code 1012 during a
  service restart), the client reconnects the same media session and preserves
  the selected mode. Because delivery is ambiguous, it never retries the interrupted
  chat automatically and asks the operator to resend it. When a successful terminal
  response contains a structured `workflow://` output ref, the client then tails the
  identity-scoped Workflow status/events HTTP facade by cursor. In interactive mode,
  `waiting_input` opens a Workflow-specific prompt, submits the response with the
  persisted resume token, and continues tailing instead of sending a new chat turn.
  Non-interactive mode stops at action-required state. On completion the client reads
  the identity-scoped `/workflows/{workflow_id}/result` artifact and prints its full
  `content`. It does not reconstruct progress or final output from legacy
  `plan.work_items`. Default workflow output is product-facing:
  the structured start response carries no mode-specific confirmation copy, the internal
  bootstrap planner is hidden, and admitted Plan progress uses persisted work-item
  `display_title` values and completion count; when multiple
  work-item runs overlap it lists the active stages as one parallel progress update, while
  hiding raw event names and workflow IDs. Use `--workflow-details` to expose
  cursor-based events and identifiers for debugging.
  当成功终包包含结构化 `task://` output ref 且显式传入 `--wait-proactive` 时，Simulator 不轮询
  Task HTTP facade，而是在同一 Agent-Service WebSocket 上等待 `durable-task:<task_id>` 主动
  `chatResponse`。服务重启导致连接关闭时，它使用相同 `sessionId + userNumber` 重新握手并继续
  等待；任务与通知由 SQLite durable task/outbox 恢复。该模式收到第一条目标提醒后结束等待，
  Ctrl-C 只停止客户端，不取消后台任务。
  Agent chat responses默认只打印流式正文，不输出 raw vendor envelope 或来源列表。
  `--citations` 显式协商 `urlCitationAnnotationsV1`，但不承担 App UI 渲染；需要检查媒体 wire
  来源时显式增加 `--citation-debug`，该参数也会自动启用 citation capability。
  The handshake marks `clientInfo.clientType=media_simulator`
  so trace and Gateway metadata can distinguish local protocol tests from
  ordinary media-agent calls. It is not a generic Gateway/Assistant client and
  uses an explicit bounded receive limit for Base64 IMAGE response frames.

For process-level keepalive, `deploy/supervisord/assistant-agent.conf` can run
`scripts/run_server.py` under `supervisord` and restart it after crashes.

## Observability and local operations

- `scripts/trace_metrics.py`: redacted trace metric summary.
- `scripts/run_runtime_audit.py`: 只读日审计稳定入口。`run` 默认审计前一北京时间自然日；Langfuse 查询
  成功但没有 Trace 时输出“昨天无运行trace”，存在异常时才调用受限 Codex。它不启动 Langfuse、不同步
  Dataset，也不运行 Agent Experiment；成功发布后清理超过 `--local-ledger-retention-days`（默认 14 天）
  且已有成功审计证明的 `.data/trace_ledger/YYYY-MM-DD.jsonl` 分片；`configure-evaluators` 管理五条 Live Observation Rule，并在真实
  回归 Dataset 已存在时管理两条 Experiment Rule。
  参数、证据边界、状态机、产物和 systemd 配置统一见
  [`docs/observability-harness.md`](../docs/observability-harness.md#langfuse-first-runtime-审计)。
- Gateway lifecycle 由 `scripts/run_server.py` 写入 `.data/gateway_events.jsonl`；仓库当前没有
  独立 viewer，按 `run_id`、`turn_id` 或 `trace_id` 使用标准 JSONL/文本工具检索。

## Eval and evidence

- `scripts/run_demo_flows.py`: offline scenario matrix for regression demos.
- `scripts/run_evals.py`: offline eval harness for lower-layer behavior checks.
- `scripts/run_system_tool_evals.py`: 真实 LLM + 真实 Tool 的 system eval；
  要求 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 和 `--allow-real-tools`，产物写入
  `.data/evals/system/tools/`。好单库购物链路可显式设置
  `MULTIMODAL_AGENT_SHOPPING_PROVIDER=haodanku` 并运行
  `shopping_search_real_single_need`。
- `scripts/run_system_shopping_eval.py`: 不经过 LLM，直接通过本地 Tool 治理链路调用真实
  `shopping_search`；要求 `--allow-real-tools`、real 模式、好单库 Provider 和 key，
  并硬断言真实候选、来源、正价格与 HTTP(S) 购买链接。
- `scripts/run_system_calendar_create_eval.py`: 不经过 LLM、`AgentGraphRuntime` 或 assistant
  loop，通过完整本地 Tool 治理链执行 `calendar_create`，验证首次提交、幂等回放和真实 SQLite
  终态。无参数默认执行，`--dry-run` 无副作用；产物写入
  `.data/evals/system/tools/calendar/create/<run>/`，不要求 real Provider mode。
- `scripts/run_system_calendar_search_eval.py`: 在 run-scoped SQLite 中通过 adapter 预置合成事件，
  再只通过完整本地 Tool 治理链执行一次 `calendar_search`，验证返回事件和只读终态。无参数默认
  执行，产物写入
  `.data/evals/system/tools/calendar/search/<run>/`。
- `scripts/run_system_context_eval.py`: 捕获真实 Runtime 编译的 `ChatRequest`
  和 Provider payload；要求 real 模式与 `--allow-unredacted-context`，产物写入
  `.data/evals/system/context/`。
- `scripts/run_system_multimodal_embedding_eval.py`: 验证本地 SigLIP2 联合 image/text ONNX
  资产。`--dry-run` 不加载模型；真实 CUDA session 必须显式传入 `--allow-local-model`，结果写入
  `.data/evals/system/multimodal_embedding/`，不保存向量、文本、图片内容或媒体路径。dry-run 还列出
  固定 5 FPS、latest-wins、纯语义选帧、VLM 文本索引和无 query-time VLM 的架构检查面；流水线行为
  由离线 pytest 验证。
- `scripts/run_release_review.py`：上线前 Release Review 的唯一稳定入口。`--inspect` 离线检查 Git YAML
  scenario；`--sync` 同步固定 LangSmith Dataset；`--configure-evaluators --model-config-id <uuid>` 默认规划
  grounding/response-quality 两条独立 Dataset rule，显式 `--apply` 才创建或更新且不运行 Judge；`--run`
  以一个原生 LangSmith Project / Experiment 执行
  Decision fixture backend 与隔离 Staging；`--record-decision` 保存 operator 的人工发布决定。真实运行
  必须同时显式允许 real Provider 和 Staging 副作用，不会静默回退 mock。Dataset、Feedback、webhook、
  清理和产物契约统一见 [`evals/README.md`](../evals/README.md)。日常 `run_runtime_audit` 不参与这条链路。
- `scripts/run_runtime_regressions.py`：Runtime Regression webhook 复用的受控执行内核。案例只来自
  Langfuse UI 中固定的 `assistant-agent-runtime-regressions` Dataset；`--preflight` 验证 Dataset Item 与
  real Provider readiness，`--run` 通过生产 `AgentGraphRuntime` 创建真实 Experiment，并等待三项
  Experiment Score 完整落库。日常操作直接在 Langfuse UI 触发，无需手工运行 CLI。流程与数据契约见
  [`evals/README.md`](../evals/README.md#日常失败到-runtime-regression)。
- `scripts/run_langsmith_runtime_regressions.py`：并行 LangSmith Runtime Regression 入口。案例只读取
  LangSmith UI 中同名固定 Dataset，不与 Langfuse 自动同步；`--inspect` 只校验 active Example object，
  `--configure-evaluators --model-config-id <uuid>` 默认只规划三个 Dataset evaluator，显式 `--apply` 才会
  创建或更新远端规则且不会运行 Judge；
  `--preflight` 校验真实 Provider 与 LangSmith exporter，`--run` 通过生产 `AgentGraphRuntime` 创建原生
  LangSmith Experiment，并等待 Runtime/LLM 子树和三项 Feedback 完整。preflight/run 都要求
  `--allow-real-provider` 与 `--allow-runtime-side-effects`。流程与 schema 见
  [`evals/README.md`](../evals/README.md#并行-langsmith-桥)。
- `scripts/run_langsmith_workflow_regressions.py`：M3 Durable Workflow 原生 LangSmith Experiment
  入口。`--inspect` 只在本地检查 typed Example、四项 Feedback 和 operator evidence 契约，不创建
  LangSmith client；`--preflight`/`--run` 必须显式允许 real Provider 与 Workflow 副作用（兼容
  `--allow-runtime-side-effects`），可用 `--env-file` 加载未跟踪配置，并检查远端 Dataset、隔离 artifact、
  shared official SQLite saver 与 production `WorkflowGraphHost` composition readiness。`--run` 直接执行
  production compiled graph，并等待真实 native tree 与四项 Feedback 完整且全部通过。固定 Dataset 不存在时
  先显式运行 `--sync`，从 Git-owned `examples.json` 幂等创建四类严格 Example。
- `scripts/run_improvement_lab.py`: offline, non-mutating improvement proposal runner.

## Specialized integrations

### SigLIP2 unified image/text embedding model

Runtime 不下载模型。先在 operator 明确允许联网和安装 export-only 依赖的环境中，把批准的
`google/siglip2-base-patch16-224` image/text projection 从同一 revision 导出为 FP16 ONNX：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/export_siglip2_embedding_onnx.py \
  --model-id google/siglip2-base-patch16-224 \
  --revision main \
  --output-dir .local/models/siglip2-base-patch16-224
```

脚本把 `main` 解析为不可变 Hugging Face commit SHA，并将共同 revision/space、图文预处理、输出维度、
两个 ONNX graph、tokenizer 及 checksum 写入 schema v2 manifest；目标目录已存在时拒绝覆盖。旧
`export_siglip2_vision_onnx.py` 只保留 deprecated 兼容入口。模型资产留在未跟踪的 `.local/`，禁止提交。

服务启用时同时显式设置 real provider mode、主 Chat/Vision 配置以及：

```bash
MULTIMODAL_AGENT_EMBEDDING_PROVIDER=local_siglip2
SIGLIP2_MODEL_DIR=.local/models/siglip2-base-patch16-224
SIGLIP2_CUDA_DEVICE_ID=0
```

安装 Runtime 可选依赖使用 `.[local-vision-embedding]`；该 extra 将 ONNX Runtime GPU 限定为
CUDA 12.8 对应的 `1.21 <= version < 1.27`，启动时还会验证实际 session 未回退到 CPU。
Runtime 使用 `onnx` 解析图中真实 external-data 引用并核对 manifest；`torch`、`transformers`
和 `onnxscript` 只属于模型准备环境，不是线上 Runtime 依赖。
旧 provider/model-dir 环境变量是迁移 alias，计划不早于 `0.3.0` 删除。完整平台边界见
`docs/multimodal-embedding-architecture.md`。

### Website guidance local verification

`website_guidance` 的 real backend 需要安装 browser extra 和对应的 Playwright Chromium；该能力默认关闭，
本地验证不需要真实 Provider 或任何 key。安装依赖与 Chromium（只在需要 real browser smoke 时执行）：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pip install -e ".[browser,dev]"
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m playwright install chromium
```

普通临时 TDD（不启动 Chromium）和受控本地 Chromium smoke 分开运行；两者都使用 mock Provider mode，
且 `tests/tdd/website_guidance/` 可由用户手动整目录删除，不属于默认 pytest。Chromium smoke 只连接测试
进程临时启动的 `127.0.0.1` HTTP server，不调用真实 Provider 或公网网站：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/website_guidance -m "not playwright_smoke"

MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/website_guidance -m playwright_smoke
```


新增脚本必须对应当前权威文档中的稳定入口或无法由现有 pytest/eval 表达的 operator 流程；
临时诊断优先使用不提交的一次性命令。
