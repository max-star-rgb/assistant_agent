# Scripts 入口索引

这里只保留当前 Agent Server、观测、评测和专项验收仍在使用的入口。一次性排障或已由 pytest、
eval、Agent Server 主链路覆盖的 probe 不应继续沉积到本目录。

- `scripts/check_documentation_authority.py`：离线校验 `docs/authority.toml` 的 owner、路由、排他事实与
  changed-path 复核范围；输出结构化 JSON，不读取 `.env`、不联网、不改写文档。

## Realtime runtime

- `scripts/run_server.py`：启动 `langgraph.json` 所声明的 Agent Server、原生 Graph 与 media custom route，
  dev backend 默认允许 10 个并发 job，避免延迟 Memory 提取占满唯一 worker 后阻塞实时聊天；可用
  `--n-jobs-per-worker` 显式调整。dev backend 可用 `--config` 显式选择独立配置；例如 Studio 演进展示使用
  `langgraph.showcase.json`，仍复用同一个单实例锁与严格端口检查。PostgreSQL backend 只接受默认配置。
  不构造项目自有 Runtime。`--backend dev`（默认）调用 `langgraph dev`，状态落在仓库共享的
  `.langgraph_api/`，只适合单个本地开发实例。wrapper 使用工作目录级单实例锁，并在启动前检查请求端口；
  端口被占用时直接失败，不允许 `langgraph dev` 自动改用随机端口。PyCharm 共享配置 **Agent Server (Real)**
  固定使用 `8089` 并启用原生 hot reload；dev bootstrap 在 WatchFiles 扫描阶段排除 `.langgraph_api/`，避免
  runtime pickle 定时落盘产生虚假的 `changes detected` 日志，同时保留 Python 源码 reload。Codex 默认作为客户端连接该实例。只有 PyCharm Server 已停止时，
  dev backend 才能临时在 `8090` 启动隔离诊断服务，并在诊断完成后停止。
  wrapper 会生成一次性 LangGraph config，使 `--env-file` 真正替换 `langgraph.json` 的 env 来源；
  `--no-env-file` 使用空 env 配置并只继承当前进程环境，可安全启动显式 mock 服务。
  `--backend postgres` 使用 `deploy/agent_server/compose.yaml` 启动专用 PostgreSQL、Redis 和本地构建的
  Agent Server 镜像，API 只绑定回环地址，PostgreSQL named volume 跨容器重启保留 Store/checkpoint。
  首次启动或代码变化后运行：

  ```bash
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
    --backend postgres --host 127.0.0.1 --port 8088 --env-file .env --rebuild
  ```

  后续启动可省略 `--rebuild`。控制台 stdout/stderr 默认同时追加到按请求端口隔离的日志：dev 位于
  系统临时目录的 `assistant_agent/logs/agent_server-<port>.log`，且拒绝仓库内路径，以免 hot reload
  watcher 消费自己的日志输出并形成反馈循环；postgres 仍位于
  `.data/logs/agent_server-<port>.log`。可用 `--log-file` 指定其他部署自有路径；旧
  `.data/logs/agent_server.log` 只保留历史记录。Studio 使用框架内建身份；其他本地客户端直接声明
  `X-Assistant-User`，当前 tokenless 部署没有网络身份认证，因此不得把 API 暴露到不受信网络。
- `scripts/install_patched_inmem_runtime.py`：从经过 SHA-256 固定的官方
  `langgraph-runtime-inmem==0.33.0` wheel 构建并安装 `0.33.0+assistant1` 本地 fork，将 dev queue 的固定
  500 ms 扫描改为 run 创建与 worker 完成事件唤醒（完成事件同时覆盖 retry），同时保留 delayed run deadline 与低频安全
  heartbeat，并在进程重启时恢复 retry counter，避免同一 run 复用 attempt 1。首次安装或重建 `hello_agent` 环境时，先安装 `.[agent-server-dev]`，再运行该脚本并完整重启
  唯一的 8089 dev server：

  ```bash
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pip install -e ".[agent-server-dev]"
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/install_patched_inmem_runtime.py
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/install_patched_inmem_runtime.py --check
  ```

  安装器不修改项目业务模块；上游 wheel digest 变化时直接失败，必须人工复核并 rebase 补丁。
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
- `scripts/agent_cli.py`：通过公开 `langgraph_sdk` 调用 Agent Server。交互模式支持 `/history` 查看脱敏
  checkpoint 元数据、`/replay <checkpoint_id>` 从历史 checkpoint 创建原生 replay 分支，以及
  `/rollback <run_id>` 用 `action="rollback"` 丢弃可取消 run；两个变更状态的命令都要求精确确认。CLI 不读取
  saver、不维护 checkpoint facade，也不承诺撤销已经发生的外部 Tool 副作用。
- `scripts/media_simulator.py`: server-backed Media-Agent protocol simulator for
  `/agent-service/v1`; type text repeatedly, or use `/new [sessionId]` to open a
  new media session. Media chat 不提供模式选择命令。If the server closes the
  connection during an interactive turn (for example close code 1012 during a
  service restart), the client reconnects the same media session. Because delivery is ambiguous, it never retries the interrupted
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

- `scripts/render_visual_perception_report.py`：从 Agent Server 日志中的脱敏
  `multimodal_observation` 事件生成自包含 HTML，按共享帧序号绘制关键帧 semantic change、选取阈值、
  reminder image-text cosine、匹配阈值和 created/triggered/cancelled 标记。日志和静态报告不包含目标文本、
  媒体内容、embedding 向量、用户原始 ID 或媒体路径，也不会为旧日志重新计算缺失值。通话中实时观察使用
  `--live`；命令只绑定 `127.0.0.1`，浏览器通过 SSE 接收日志追加事件，并显示最新 semantic keyframe 大图
  与最近 12 帧缩略图时间轴。图片由独立的受限本地路由从
  `.data/visual_perception/keyframes/semantic-input/agent-service-video-<session-hash-24>/` 按需读取，
  并要求当前日志快照已存在同帧 `semantic_frame.selected`；图片不写入日志或 SSE。需要覆盖 `keyframes`
  根目录时使用 `--keyframe-root`。日志轮转或连接中断后自动恢复。
  实时模式省略 `--session-digest` 时自动跟随最新产生视觉事件的会话并切换页面：

  ```bash
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
    scripts/render_visual_perception_report.py \
    --log-file /tmp/assistant_agent/logs/agent_server-8089.log \
    --live --open
  ```

  不传 `--live` 时生成静态快照，默认输出到
  `.data/diagnostics/visual-perception-<session-digest>.html`；实时服务默认端口为 `8765`，可用 `--port`
  修改。实时模式显式传入 `--session-digest` 会固定在该会话；静态模式仍必须传入 digest。
- 生产 Graph 生命周期以 Agent Server/LangSmith native trace 为准；旧 Gateway JSONL 兼容观测模块已删除。

## Eval and evidence

- `evals/system/tools/<tool_name>.py`: 每个当前注册 Tool 一个可在 PyCharm 直接运行的离线固定输入冒烟脚本；
  通过原生 `ToolNode` 调用一次，只检查调用成功且返回结果，不断言候选数量、排序或具体业务内容。
  `scripts/run_system_calendar_create_eval.py` 与 `scripts/run_system_calendar_search_eval.py` 仅保留为相应脚本的
  兼容 PyCharm 入口。
- `evals/system/tools/run_all.py`: 可在 PyCharm 中直接运行；递归发现并依次运行目录下除 helper 与自身之外的
  全部 Tool 冒烟脚本，最后输出逐脚本 return code 和聚合通过状态。
- `scripts/run_system_multimodal_embedding_eval.py`: 验证本地 SigLIP2 联合 image/text ONNX
  资产。`--dry-run` 不加载模型；真实 CUDA session 必须显式传入 `--allow-local-model`，结果写入
  `.data/evals/system/multimodal_embedding/`，不保存向量、文本、图片内容或媒体路径。dry-run 还列出
  原始帧 SigLIP2 latest-pending、1 FPS 关键帧保底、纯语义选帧、关键帧独立并行 VLM、VLM 文本索引和无 query-time VLM
  的架构检查面；流水线行为
  由离线 pytest 验证。
- `scripts/run_system_realtime_visual_target_window_eval.py`：验证 live camera SigLIP2 latest-wins、chat
  冻结严格 1～5 个逻辑关键帧 sequence、只等待最后一个 target 的行为。默认
  `--dry-run` 不读取图片、不联网；真实运行要求 real Provider mode、完整 Qwen realtime vision 配置、
  `--allow-real-provider` 和 operator 提供的恰好 5 张 sequence-named JPEG。每个实际执行的关键帧使用隔离 WebSocket
  client，已选关键帧并行执行，Tool 只等待逻辑窗口的 exact target；artifact 只保存 sequence/status/latency/concurrency 和 trace/span ID。
- 旧 Runtime/Workflow/Release Review LangSmith runner 已随旧 Graph Runtime 删除。后续评测重建必须直接消费
  Agent Server 或 `NativeGraphEvaluationTarget` 的标准 messages/native trace，当前不得宣称存在上线前行为门禁。

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
`docs/visual-perception-architecture.md`。

### Website guidance real backend setup

Website guidance 的 real backend 依赖 `pyproject.toml` 中的 `browser` extra 和 Playwright Chromium：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pip install -e ".[browser]"
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m playwright install chromium
```

运行时还必须显式设置 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 和
`MULTIMODAL_AGENT_WEBSITE_GUIDANCE_ENABLED=true`。`WEBSITE_GUIDANCE_NAVIGATION_TIMEOUT_SECONDS` 可选，
只接受 `(0, 30]` 秒，默认 10 秒。依赖或 Chromium 未就绪时 plugin fail closed，不注册真实 browser Tool。

新增脚本必须对应当前权威文档中的稳定入口或无法由现有 pytest/eval 表达的 operator 流程；
临时诊断优先使用不提交的一次性命令。
