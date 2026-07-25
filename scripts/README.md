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

- `scripts/check_env.py`: environment sanity check.
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
- `scripts/run_langfuse_agent_evals.py`: optionally seeds the first Dataset, then
  runs either the default scripted mock baseline or the explicit
  `--real-readonly --allow-real-tools` five-case real Chat Provider/weather
  profile through a Langfuse Experiment and shared Runtime trace. Scoring is
  asynchronous and owned by Langfuse-native Code and LLM-as-a-Judge
  Evaluators; the script does not register SDK evaluators. It loads the
  untracked `.env` by default. Case implementation lives under
  `evals/cases/langfuse/`.
- `scripts/run_improvement_lab.py`: offline, non-mutating improvement proposal runner.
- `scripts/check_pilot_readiness.py` and `scripts/collect_pilot_evidence.py`:
  multi-agent pilot operator helpers.

## Specialized integrations


新增脚本必须对应当前权威文档中的稳定入口或无法由现有 pytest/eval 表达的 operator 流程；
临时诊断优先使用不提交的一次性命令。
