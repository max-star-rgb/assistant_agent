# Scripts 入口索引

这里只保留当前 Agent Server、观测、评测和专项验收仍在使用的入口。一次性排障或已由 pytest、
eval、Agent Server 主链路覆盖的 probe 不应继续沉积到本目录。

- `scripts/check_documentation_authority.py`：离线校验 `docs/authority.toml` 的 owner、路由、排他事实与
  changed-path 复核范围；输出结构化 JSON，不读取 `.env`、不联网、不改写文档。

## Realtime runtime

- `scripts/run_server.py`：调用本环境的 `langgraph dev` 启动 `langgraph.json` 所声明的
  Agent Server、原生 Graph 与 media custom route；它只负责 host/port/reload/env-file 参数，不构造项目自有
  Runtime，也不打印旧 runtime completeness 或 Gateway lifecycle 摘要。控制台 stdout/stderr 默认同时追加到
  `.data/logs/agent_server.log`，可用 `--log-file` 指定其他部署自有路径。LangSmith Studio 使用框架内建
  `StudioUser` 认证；real mode 的 CLI/media 等非 Studio 客户端才需要项目 service token 与签名。
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
  不是 Runtime Memory Plugin 管理入口，也不经过 `assistant_memory_plugin_v1` 的
  `open_session` / `prepare_context` / `ingest_turn` / `close_session` 生命周期。

Runtime Memory Plugin 的只读装配诊断不是 `scripts/` supervisor，直接运行：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m assistant_agent.memory.cli plugins
```

该命令只解析配置并装配 factory，输出脱敏 JSON；不启动 Mem0/Qdrant，不执行远端健康检查、召回、
写入或真实 Provider 请求。默认 mock 会报告 sealed 的 `mem0` slot，同时以
`readiness=unavailable` 和 `memory_plugin_offline` 明确表示后端离线。
- `scripts/migrate_mem0_memories_to_chinese.py`：检查或迁移一个 runtime 用户已有的
  Mem0 记忆为简体中文。默认命令只读；更新要求 real Provider mode、已配置的 Qwen 和
  Mem0，并同时传入 `--apply` 与 `--allow-real-provider`。输出只包含数量、memory ID、
  状态和稳定错误码，不持久化记忆正文或 Provider 响应。
- `scripts/agent_cli.py`：通过公开 `langgraph_sdk` 调用 Agent Server 的 thread/run/stream/cancel CLI。
- `scripts/media_simulator.py`: server-backed Media-Agent protocol simulator for
  `/agent-service/v1`; type text repeatedly, or use `/new [sessionId]` to open a
  new media session. In interactive mode, `/planning` selects planning mode for
  subsequent turns and `/fast` switches back;
  these local commands are not sent as chat text. If the server closes the
  connection during an interactive turn (for example close code 1012 during a
  service restart), the client reconnects the same media session and preserves
  the selected mode. Because delivery is ambiguous, it never retries the interrupted
  chat automatically and asks the operator to resend it. When a successful terminal
  response arrives it prints only the Agent Server-projected media result; it does not
  poll a parallel Workflow facade.
  当成功终包包含结构化 `task://` output ref 且显式传入 `--wait-proactive` 时，Simulator 不轮询
  Task HTTP facade，而是在同一 Agent-Service WebSocket 上等待 `durable-task:<task_id>` 主动
  `chatResponse`。服务重启导致连接关闭时，它使用相同 `sessionId + userNumber` 重新握手并继续
  等待；任务与通知由 SQLite durable task/outbox 恢复。该模式收到第一条目标提醒后结束等待，
  Ctrl-C 只停止客户端，不取消后台任务。
  Agent chat responses默认只打印流式正文，不输出 raw vendor envelope 或来源列表。
  `--citations` 显式协商 `urlCitationAnnotationsV1`，但不承担 App UI 渲染；需要检查媒体 wire
  来源时显式增加 `--citation-debug`，该参数也会自动启用 citation capability。
  The handshake marks `clientInfo.clientType=media_simulator`
  so trace and Agent Server custom-route metadata can distinguish local protocol tests from
  ordinary media-agent calls. It is not a generic Agent Server client and
  uses an explicit bounded receive limit for Base64 IMAGE response frames.

For process-level keepalive, `deploy/supervisord/assistant-agent.conf` can run
`scripts/run_server.py` under `supervisord` and restart it after crashes.

## Observability and local operations

- `scripts/trace_metrics.py`: redacted trace metric summary.
- 生产 Graph 生命周期以 Agent Server/LangSmith native trace 为准。`.data/gateway_events.jsonl` 只属于仍显式
  启用旧兼容观测模块的外围入口，不由 `scripts/run_server.py` 自动生成，也不能作为原生 Graph 事实源。

## Eval and evidence

- `scripts/run_demo_flows.py`: offline scenario matrix for regression demos.
- `scripts/run_evals.py`: offline eval harness for lower-layer behavior checks.
- `scripts/run_system_calendar_create_eval.py`: 不经过 LLM 或 Assistant Graph，通过最小 StateGraph 的原生
  `ToolNode` 执行 `calendar_create`，验证首次提交、幂等回放和真实 SQLite
  终态。无参数默认执行，`--dry-run` 无副作用；产物写入
  `.data/evals/system/tools/calendar/create/<run>/`，不要求 real Provider mode。
- `scripts/run_system_calendar_search_eval.py`: 在 run-scoped SQLite 中通过 adapter 预置合成事件，
  再通过最小 StateGraph 的原生 `ToolNode` 执行一次 `calendar_search`，验证返回事件和只读终态。无参数默认
  执行，产物写入
  `.data/evals/system/tools/calendar/search/<run>/`。
- `scripts/run_system_multimodal_embedding_eval.py`: 验证本地 SigLIP2 联合 image/text ONNX
  资产。`--dry-run` 不加载模型；真实 CUDA session 必须显式传入 `--allow-local-model`，结果写入
  `.data/evals/system/multimodal_embedding/`，不保存向量、文本、图片内容或媒体路径。dry-run 还列出
  固定 5 FPS、latest-wins、纯语义选帧、VLM 文本索引和无 query-time VLM 的架构检查面；流水线行为
  由离线 pytest 验证。
- 旧 Runtime/Workflow/Release Review LangSmith runner 已随旧 Graph Runtime 删除。后续评测重建必须直接消费
  Agent Server 或 `NativeGraphEvaluationTarget` 的标准 messages/native trace，当前不得宣称存在上线前行为门禁。
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
