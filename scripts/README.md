# Scripts 入口索引

这里只保留当前 runtime、观测、评测和专项验收仍在使用的入口。一次性排障或已由 pytest、
eval、Gateway 主链路覆盖的 probe 不应继续沉积到本目录。

## Realtime runtime

- `scripts/run_server.py`: starts the FastAPI backend with Gateway, media, HTTP,
  memory, trace, and tool-governed runtime routes.
- `scripts/run_langfuse.py`: PyCharm-friendly local Langfuse supervisor. It starts
  the ignored `.data/langfuse` Compose stack, waits for health, stays attached as
  one Run process, and stops the containers without deleting data when terminated.
- `scripts/run_mem0.py`: starts the repository-local Mem0 stack (Mem0 + Qdrant),
  waits until Mem0 is healthy, and then exits while leaving both containers
  running. It reuses local images and persistent Compose volumes without building,
  pulling, or clearing stored memory.
- `scripts/run_client.py`: server-backed Media-Agent protocol console client for
  `/agent-service/v1`; type text repeatedly, or use `/new [sessionId]` to open a
  new media session. Agent chat responses print only the reply text, not the
  raw vendor envelope. The handshake marks `clientInfo.clientType=run_client`
  so trace and Gateway metadata can distinguish local protocol tests from
  ordinary media-agent calls.

For process-level keepalive, `deploy/supervisord/assistant-agent.conf` can run
`scripts/run_server.py` under `supervisord` and restart it after crashes.

## Observability and local operations

- `scripts/gateway_view.py`: Gateway lifecycle JSONL viewer.
- `scripts/agentruntime_view.py`: canonical runtime trace viewer.
- `scripts/trace_metrics.py`: redacted trace metric summary.

## Eval and evidence

- `scripts/run_demo_flows.py`: offline scenario matrix for regression demos.
- `scripts/run_evals.py`: offline eval harness for lower-layer behavior checks.
- `scripts/run_system_tool_evals.py`: 真实 LLM + 真实 Tool 的 system eval；
  要求 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 和 `--allow-real-tools`，产物写入
  `.data/evals/system/tools/`。
- `scripts/run_system_context_eval.py`: 捕获真实 Runtime 编译的 `ChatRequest`
  和 Provider payload；要求 real 模式与 `--allow-unredacted-context`，产物写入
  `.data/evals/system/context/`。
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
  `.data/evals/remote/`。本机 Langfuse 通过内部 80 端口
  `assistant-agent-eval-webhook` 转发到 Assistant Server 8089；配置与操作步骤见 `evals/README.md`。
- `scripts/run_improvement_lab.py`: offline, non-mutating improvement proposal runner.
- `scripts/check_pilot_readiness.py` and `scripts/collect_pilot_evidence.py`:
  multi-agent pilot operator helpers.

## Specialized integrations


新增脚本必须对应当前权威文档中的稳定入口或无法由现有 pytest/eval 表达的 operator 流程；
临时诊断优先使用不提交的一次性命令。
