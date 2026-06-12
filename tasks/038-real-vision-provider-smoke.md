# Task 038 真实 Vision Provider Smoke Test 准备

## Goal

为真实 Vision Provider 增加安全的本地 smoke test 入口，但不默认调用真实 Provider。

## Read first

- `docs/36-phase4-5-real-provider-smoke.md`
- `docs/37-api-key-and-env-safety.md`
- `docs/38-demo-data-and-smoke-flow.md`
- 当前 `src/multimodal_agent/config.py`
- 当前 `src/multimodal_agent/services/*vision*adapter*.py`
- 当前 `src/multimodal_agent/agent/runtime.py`

## Scope

新增或完善：

```text
.env.example
scripts/smoke_real_vision.py
```

## Requirements

- `.env.example` 只能包含变量名和占位说明，不得包含真实 key。
- smoke 脚本缺少 API Key 时清晰提示并退出。
- smoke 脚本不得在 import 时自动调用 Provider。
- smoke 脚本只有用户显式执行时才运行。
- 默认 `python -m pytest` 不调用真实 Provider。
- 不自动安装依赖。
- 不写入 `.env` 或 `.env.local`。
- 不提交真实图片或视频大文件。

## Suggested behavior

```bash
python scripts/smoke_real_vision.py --image demo_data/images/shoe.jpg
```

缺少配置时输出：

```text
provider_unconfigured
Please set MULTIMODAL_AGENT_VISION_PROVIDER and provider API key.
```

配置完整时：

- 构造 UserRequest。
- 调用 AgentGraphRuntime。
- 打印 response_text、tool_calls、errors、trace_id。
- 不打印 API Key。

## Tests

新增或更新：

```text
tests/test_smoke_real_vision_script.py
```

覆盖：

- 缺少 API Key 时不崩溃。
- 默认不会调用真实 Provider。
- 脚本 help 可运行。
- `.env.example` 不包含真实 secret 模式。

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 039。
