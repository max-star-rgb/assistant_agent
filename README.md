# Multimodal Agent

An assistant-driven multimodal agent that routes text, image, video, product, render, and memory requests through structured local capabilities.

The default project mode is mock/local/offline. You can run the CLI, API, Web Console, tests, evals, demo flows, MCP smoke, and skill validation without API keys or real external Providers.

Phase 6 Productization / Usable Demo is complete. See [Phase 6 Review](docs/phase1-7/121-phase6-productization-review.md) for the final release audit.

## Quick Start

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"   # 本地离线直跑，不连服务器
python scripts/run_demo_flows.py
```

## Server + Clients

The assistant runs as **one backend server** that two clients talk to. The
server owns all provider/env configuration; the clients just send requests.

1. Start the backend server:

   ```bash
   python scripts/run_server.py
   ```

   For auto-reload during local development:

   ```bash
   python scripts/run_server.py --reload
   ```

   To let other devices on the same network try the Web Console, bind the
   server to all interfaces and share your machine's LAN IP URL:

   ```bash
   python scripts/run_server.py --host 0.0.0.0 --trial-user-id alice,bob
   # Share: http://<your-machine-ip>:8000/demo/console
   ```

   You can also load allowed trial ids from an untracked local file:

   ```bash
   python scripts/run_server.py --host 0.0.0.0 --trial-user-id-file .local/trial-users.txt
   ```

   To use a real chat provider, keep credentials in your local shell or
   untracked `.env` and pass the provider to the **server**:

   ```bash
   python scripts/run_server.py --provider deepseek
   ```

2. Use either client against the running server:

   - **Web Console** (browser client): open
     `http://127.0.0.1:8000/demo/console`
   - **CLI client** (`scripts/run_client.py`): streams over the same WebSocket
     endpoint as the Web Console

     ```bash
     python scripts/run_client.py --server http://127.0.0.1:8000 "你好"
     ```

Health check and console URLs:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/demo/console
```

## What It Can Do

Current assistant capabilities:

- `direct_chat`
- `image_generation`
- `image_understanding`
- `video_understanding`
- `product_search`
- `price_compare`
- `render_3d`
- `memory_retrieval`
- `multi_step_orchestration`

The agent owns intent routing, planning, tool selection, graph execution, response composition, trace/debug output, and safety boundaries. Specific capabilities are implemented behind adapters and tools.

## Agent Tool Calling Architecture

The modern ReAct path uses provider-native tool calling for non-mock chat providers:

```text
ToolSpec
  -> provider tools schema
  -> model proposes native tool_calls
  -> AssistantDecision
  -> ActionValidator / policy / budget
  -> ToolExecutor
  -> ToolObservation
  -> model final_answer
```

The model proposes actions; local code still owns validation, execution, loop guards, trace redaction, and memory boundaries. `ASSISTANT_TOOL_CALL_MODE=auto` is the default: mock/offline runs keep the deterministic compatibility path, while real chat providers prefer native `tool_calls`. `prompt_json` remains an explicit fallback for debugging and older providers.

## Main Entry Points

CLI:

```bash
python scripts/run_assistant_cli.py --text "生成一张日系极简海报"
python scripts/run_assistant_cli.py --scenario product_search_compare
```

Offline demo runner:

```bash
python scripts/run_demo_flows.py
python scripts/run_demo_flows.py --scenario image_generation_basic
python scripts/run_demo_flows.py --scenario full_multistep_image_search_compare_generate
```

Manual Qwen image generation smoke is opt-in and uses DashScope only when you explicitly select the provider:

```bash
export MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
export MULTIMODAL_AGENT_IMAGE_PROVIDER=qwen
export QWEN_IMAGE_API_KEY=<your-local-key>
python scripts/smoke_text_image_generation.py --prompt "生成一张白色运动鞋的电商主图"
```

Native tool-calling smoke defaults to a local scripted adapter and does not call real providers:

```bash
python scripts/smoke_native_tool_calling.py --query "帮我找一款通勤蓝牙耳机"
```

Real chat provider native tool-calling smoke is opt-in:

```bash
export MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
export MULTIMODAL_AGENT_CHAT_PROVIDER=deepseek
export DEEPSEEK_CHAT_API_KEY=<your-local-key>
python scripts/smoke_native_tool_calling.py --real-provider --query "帮我找一款通勤蓝牙耳机"
```

API:

```text
GET  /health
GET  /demo/scenarios
GET  /demo/console
POST /agent/run
GET  /runs/{run_id}
GET  /traces/{trace_id}
GET  /runs/{run_id}/tool-calls
```

Local Docker demo:

```bash
docker compose up --build
```

## Documentation

Start here:

- Codex docs: [Project Guide](docs/CODEX_PROJECT_GUIDE.md), [Docs Index](docs/DOCS_INDEX.md), [Tests Review](docs/TESTS_REVIEW.md)
- [Quickstart](docs/quickstart.md)
- [Architecture](docs/architecture.md)
- [Architecture Layers](docs/architecture-layers.md)
- [Capabilities](docs/capabilities.md)
- [Configuration](docs/configuration.md)
- [Provider Setup](docs/provider-setup.md)
- [Demo Flows](docs/demo-flows.md)
- [Local Deployment](docs/deployment-local.md)
- [Development](docs/development.md)
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Release Checklist](docs/release-checklist.md)

Historical phase docs remain in `docs/` and task specs remain in `tasks/`, but ordinary users should not need to read them.

## Safety Defaults

- Default Providers are mock/local.
- Default tests do not call real Providers.
- Default evals do not call real Providers.
- Default demo flows do not call real Providers.
- API keys must only live in a local shell or untracked local env file.
- Do not commit real media, generated images, rendered artifacts, raw Provider responses, logs, or secrets.

## Release Checks

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
git status --short
```
