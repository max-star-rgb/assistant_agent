# 126 Phase 7A Runtime Configuration Profiles

## Why This Phase Exists

你现在已经有一个可运行 demo：

- CLI 可跑
- API / Web Console 可跑
- demo flows 可跑
- mock/local 默认安全
- 真实 Provider 有 opt-in 文档
- Docker/local deployment 有基础

但下一步如果直接接真实 Provider、做 Web 产品化或试点用户，很容易出现一个问题：

```text
到底当前运行是在 demo 模式、测试模式、真实 Provider smoke 模式，还是试点生产模式？
```

Phase 7A 的目的就是先把运行模式讲清楚、写进配置、加测试锁住边界。

## One Sentence Goal

建立统一的 runtime profile，让项目明确区分：

```text
local_demo
offline_eval
provider_smoke
pilot
```

并保证默认永远是 `local_demo`，不会意外调用真实 Provider。

## What You Should Do Now

先不要做真实 Provider 深度接入。
先不要做复杂前端。
先不要做用户登录。

现在应该做：

```text
Task 124 Runtime Profile Schema and Defaults
```

也就是：

1. 定义 RuntimeProfile schema。
2. 从环境变量读取 profile。
3. 默认 profile 是 `local_demo`。
4. 明确每个 profile 是否允许真实 Provider。
5. 测试默认 pytest / eval / demo 都还是离线。

## Runtime Profiles

### 1. local_demo

默认模式。

用途：

- 本地 CLI
- Web Console
- demo flows
- 普通开发调试

规则：

- Provider 默认 mock/local。
- 不调用真实 Provider。
- 不需要 API Key。
- 可以使用本地 memory。
- 可以查询 run / trace。

示例：

```bash
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
uvicorn multimodal_agent.api.app:app --host 127.0.0.1 --port 8000 --reload
```

### 2. offline_eval

离线评测模式。

用途：

- `python scripts/run_evals.py`
- CI-like local checks
- 回归测试

规则：

- 强制 mock/local。
- 禁止真实 Provider。
- 输出必须可复现。
- 不依赖 API Key。

示例：

```bash
MULTIMODAL_AGENT_RUNTIME_PROFILE=offline_eval python scripts/run_evals.py
```

### 3. provider_smoke

真实 Provider 手动 smoke 模式。

用途：

- 用户手动验证 Qwen/OpenAI/local provider
- 单能力 smoke

规则：

- 只有显式设置该 profile 才允许真实 Provider。
- 必须显式设置 Provider env。
- 缺 key/base_url/model 时返回清晰错误。
- 不回退 mock 来伪装成功。
- 不提交真实输出。

示例：

```bash
export MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
export MULTIMODAL_AGENT_VISION_PROVIDER=qwen
export QWEN_API_KEY="<set-in-local-shell>"
python scripts/smoke_real_vision.py --image <local-image-path>
```

### 4. pilot

小范围真实试点模式。

用途：

- 后续给真实用户试用
- 真实 Provider 可控启用
- 更严格 trace、cost、auth 边界

规则：

- 不能是默认模式。
- 必须显式配置。
- 必须通过 Provider readiness check。
- 应该配合用户/session 边界。
- 应该配合成本限制。

Phase 7A 只定义这个 profile，不实现完整试点系统。

## Proposed Environment Variable

新增：

```text
MULTIMODAL_AGENT_RUNTIME_PROFILE=local_demo
```

允许值：

```text
local_demo
offline_eval
provider_smoke
pilot
```

默认：

```text
local_demo
```

## Proposed Data Model

建议新增：

```text
src/multimodal_agent/runtime_profile.py
```

或如果你想更集中，也可以放在：

```text
src/multimodal_agent/config.py
```

推荐模型字段：

```text
name
allows_real_providers
allows_network_provider_calls
requires_explicit_provider_config
default_provider_mode
description
```

示例语义：

| profile | allows_real_providers | default_provider_mode |
| --- | --- | --- |
| `local_demo` | false | mock |
| `offline_eval` | false | mock |
| `provider_smoke` | true | explicit |
| `pilot` | true | explicit |

## Minimal Implementation Plan

### Task 124 Runtime Profile Schema and Defaults

Scope:

- 新增 RuntimeProfile 枚举或 Pydantic model。
- 新增 `get_runtime_profile()` 或 `RuntimeProfile.from_env()`。
- `.env.example` 增加 `MULTIMODAL_AGENT_RUNTIME_PROFILE=local_demo`。
- 测试默认 profile 是 `local_demo`。
- 测试未知 profile 报清晰错误。

Acceptance:

```bash
python -m pytest
```

### Task 125 Wire Runtime Profile into ProviderConfig

Scope:

- `ProviderConfig.from_env()` 读取 runtime profile。
- `local_demo` 和 `offline_eval` 下默认 Provider 保持 mock/local。
- `provider_smoke` / `pilot` 不自动启用真实 Provider，只允许显式 Provider 配置生效。
- 测试默认路径不变。

Acceptance:

```bash
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
```

### Task 126 Runtime Profile Safety Tests

Scope:

- 测试 `offline_eval` 禁止真实 Provider selector。
- 测试 `provider_smoke` 缺配置时不回退 mock。
- 测试默认 CLI/API/Web/demo 仍离线。
- 测试错误信息不泄露 key/token。

Acceptance:

```bash
python -m pytest
```

### Task 127 Runtime Profile Docs and Review

Scope:

- 更新 `docs/configuration.md`。
- 更新 `docs/provider-setup.md`。
- 更新 `.env.example`。
- 新增 Phase 7A review。

Acceptance:

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
```

## Files You Will Probably Touch

Likely source files:

```text
src/multimodal_agent/config.py
src/multimodal_agent/runtime_profile.py
```

Likely tests:

```text
tests/test_runtime_profile.py
tests/unit/test_provider_config.py
tests/test_provider_selection.py
```

Likely docs:

```text
.env.example
docs/configuration.md
docs/provider-setup.md
docs/126-phase7a-runtime-configuration-profiles.md
```

## What Not To Do In Phase 7A

Do not:

- 接真实 Provider 默认调用。
- 做前端产品化。
- 做登录系统。
- 做部署服务器。
- 做 Kubernetes。
- 做真实用户试点。
- 改 capability 行为。
- 改意图识别质量。
- 改 Web Console 大功能。

Phase 7A 只解决一个问题：

```text
当前运行模式是什么？这个模式允许什么？禁止什么？
```

## Success Criteria

Phase 7A 完成时应满足：

- 不设置环境变量时，profile 是 `local_demo`。
- 默认 pytest 仍离线。
- 默认 eval 仍离线。
- 默认 demo flows 仍离线。
- 真实 Provider 只能在 `provider_smoke` 或 `pilot` profile 下显式启用。
- 缺真实 Provider 配置时，返回清晰错误，不伪装成 mock 成功。
- 文档能解释用户该用哪个 profile。

## Recommended Next User Prompt

如果你要继续推进，可以直接让 Codex 执行：

```text
请先阅读 AGENTS.md、docs/125-phase7-production-readiness-roadmap.md、docs/126-phase7a-runtime-configuration-profiles.md。

现在进入 Phase 7A Runtime Configuration Profiles。
请从 Task 124 Runtime Profile Schema and Defaults 开始。

执行规则：
- 每次只执行一个 task。
- 不跨 task 实现。
- 修改源码、测试、文档优先使用 apply_patch。
- 不写 API Key。
- 不调用真实 Provider。
- 默认运行路径必须 mock/local/offline。
- 完成后运行 python -m pytest 并停止。
```
