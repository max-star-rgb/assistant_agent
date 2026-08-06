# Scripts 入口索引

这里只保留当前 runtime、观测、评测和专项验收仍在使用的入口。一次性排障或已由 pytest、
eval、Gateway 主链路覆盖的 probe 不应继续沉积到本目录。

## Realtime runtime

- `scripts/run_server.py`: starts the FastAPI backend with Gateway, media, HTTP,
  memory, trace, and tool-governed runtime routes.
  本机 Langfuse 需要查看 Mem0 具体 change text 时，显式增加
  `--allow-local-memory-trace-content`；它只对 loopback OTLP endpoint 生效，canonical JSONL
  仍只保留数量、event 计数和 memory ID。在 Langfuse Session 中打开各 turn 的
  `memory.turn_ingestion` 查看结果，单条演化用 Mem0 原生 history API 钻取。
- `scripts/run_langfuse.py`: PyCharm-friendly local Langfuse supervisor. It starts
  the ignored `.data/langfuse` Compose stack, waits for health, stays attached as
  one Run process, and stops the containers without deleting data when terminated.
- `scripts/run_mem0.py`: PyCharm-friendly local Mem0 operator console. It starts
  Mem0 + Qdrant, waits for health, then stays attached at a `mem0> ` prompt with
  `help`, `status`, `list`, `get`, `history`, `add`, `update`, `delete`, `clear`,
  and `exit` commands. The console directly displays and manages raw records across
  all Mem0 identities; `add` stores text directly by default and only enables
  Mem0 inference with `add --infer`. Single-record deletion requires confirmation
  unless `--yes` is supplied. `clear` accepts one or more raw identity filters;
  `clear --all` resets every memory plus Mem0 history and always requires typing
  `DELETE ALL MEMORIES` (`--yes` is rejected for this scope). Exiting leaves both
  containers and persistent data running.
- `scripts/migrate_mem0_memories_to_chinese.py`：检查或迁移一个 runtime 用户已有的
  Mem0 记忆为简体中文。默认命令只读；更新要求 real Provider mode、已配置的 Qwen 和
  Mem0，并同时传入 `--apply` 与 `--allow-real-provider`。输出只包含数量、memory ID、
  状态和稳定错误码，不持久化记忆正文或 Provider 响应。
- `scripts/run_client.py`: server-backed Media-Agent protocol console client for
  `/agent-service/v1`; type text repeatedly, or use `/new [sessionId]` to open a
  new media session. Agent chat responses print only the reply text, not the
  raw vendor envelope. The handshake marks `clientInfo.clientType=run_client`
  so trace and Gateway metadata can distinguish local protocol tests from
  ordinary media-agent calls. It is not a generic Gateway/Assistant client and
  uses an explicit bounded receive limit for Base64 IMAGE response frames.

For process-level keepalive, `deploy/supervisord/assistant-agent.conf` can run
`scripts/run_server.py` under `supervisord` and restart it after crashes.

## Observability and local operations

- `scripts/trace_metrics.py`: redacted trace metric summary.
- `scripts/run_runtime_audit.py`: 只读日审计稳定入口。默认 `run` 审计前一北京时间自然日，并从
  watermark 顺序补齐漏掉的日期；Langfuse 是主证据，本地 trace 只做完整性与有限 fallback。人读结果只写
  `.data/runtime_audit/reports/YYYY-MM-DD.md`，内部 JSON 留在 inbox/state；已确认的空日生成极简中文日报且
  不调用 Codex。按日期复查或其他完整参数见 `--help` 与
  [`docs/observability-harness.md`](../docs/observability-harness.md)。
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
- `scripts/run_agent_evals.py`: Task 中心的 Agent eval 稳定入口。`--inspect`
  只读显示 Task 和 Environment；`--calibrate` 直接校准隐藏 grader；
  `--publish` 把所选 Task 薄发布到统一 Langfuse Dataset；`--run` 通过活动
  `AgentGraphRuntime` 创建 Experiment、Trace 和 `agent_eval.*` Score。用可重复
  `--task` 精确选择，或用 `--suite` 选择集合。真实 Chat 调用同时要求 real 模式、
  完整 Provider 配置和 `--allow-real-provider`。Judge 默认非流式、30 秒 timeout、0 次 SDK
  retry，并通过 `ipv4_direct` 绕过代理、强制 IPv4；可用 `--judge-timeout-seconds`、
  `--judge-max-retries` 覆盖传输边界，或用 `--judge-network-mode environment` 恢复环境代理和
  DNS；阶段进度写 stderr。
  实现位于 `evals/agent/`。
- Langfuse Remote Custom Experiment 可调用 Assistant Server 的
  `POST /internal/evals/langfuse/remote-experiment`；该默认关闭的 HMAC webhook 只把已校验的
  Task/Suite 映射为上述 CLI 的固定后台 argv，运行回执和 stdout/stderr 写入
  `.data/evals/remote/`。本机固定的 Langfuse `3.224.2` 通过内部 80 端口
  `assistant-agent-eval-webhook` 转发到 Assistant Server 8089；代理为该版本尚未签名的
  Remote Experiment 请求补充共享 HMAC。空 `payload` 通过 `--dataset-active` 运行统一 Dataset
  中全部 ACTIVE Git Task；请求契约、secret 配置与操作步骤见 `evals/README.md`。UI 触发的新运行
  只在 Assistant Server 控制台显示单个 `tqdm` 整体任务进度条，不打印 Task、evaluation、Judge
  阶段明细或结束状态。状态查询和停止命令仍可直接输入，但服务器启动时不额外打印命令提示：

  ```bash
  eval status
  eval stop
  eval status <trigger_id>
  eval stop <trigger_id>
  ```

  不传 `trigger_id` 时默认操作最新运行。`eval stop` 发送 `SIGTERM` 前会核对回执中的完整启动命令
  和独立进程组，旧版无 `command` 回执不会被停止。
- `scripts/run_improvement_lab.py`: offline, non-mutating improvement proposal runner.
- `scripts/check_pilot_readiness.py` and `scripts/collect_pilot_evidence.py`:
  multi-agent pilot operator helpers.

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
