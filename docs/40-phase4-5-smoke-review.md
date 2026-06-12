# 40 Phase 4.5 Smoke Test 审计报告

## 结论

Phase 4.5 已完成真实 Vision Provider smoke test 的安全准备，但未默认调用真实 Provider。当前仓库具备手动 smoke 入口、环境变量模板、低风险 demo data 目录和本地运行说明。

默认开发、默认 pytest 和离线 eval 仍使用 Mock/Local 路径，不依赖外部服务或 API Key。

## 1. 默认是否仍使用 MockAdapter

是。

`ProviderConfig.from_env()` 在未设置 `MULTIMODAL_AGENT_VISION_PROVIDER=openai|qwen` 时默认使用 `vision_provider="mock"`。`create_vision_adapter()` 在 mock 配置下返回 `MockVisionUnderstandingAdapter`。

默认 `AgentGraphRuntime()` 仍通过默认配置创建 mock registry，因此默认请求不会调用真实 Vision Provider。

## 2. 是否存在 `.env.example`

是。

`.env.example` 已列出：

- `MULTIMODAL_AGENT_VISION_PROVIDER`
- `OPENAI_API_KEY`
- `OPENAI_VISION_BASE_URL`
- `OPENAI_VISION_MODEL`
- `QWEN_API_KEY`
- `QWEN_VISION_BASE_URL`
- `QWEN_VISION_MODEL`
- `RUN_INTEGRATION_TESTS`

文件只包含变量名和占位说明，不包含真实 key。

## 3. 是否存在 smoke 脚本

是。

脚本路径：

```text
scripts/smoke_real_vision.py
```

该脚本只有在用户显式运行时才进入 `main()`；import 脚本不会调用 Provider。测试已覆盖 import 不触发 `urllib.request.urlopen`。

## 4. 缺 key 时是否清晰退出

是。

当 provider 未设置为 `openai`/`qwen`，或缺少对应 API Key 时，脚本会输出：

```text
provider_unconfigured
missing OPENAI_API_KEY
Please set MULTIMODAL_AGENT_VISION_PROVIDER and provider API key.
```

或对应的 Qwen key 提示，并以退出码 `2` 结束。该路径在构造 `AgentGraphRuntime` 之前返回，不会调用真实 Provider。

## 5. 默认 pytest 是否离线

是。

默认测试：

```bash
python -m pytest
```

不需要 API Key，不调用真实 Provider。`tests/integration` 默认 skip，只有 `RUN_INTEGRATION_TESTS=1` 时才进入真实集成测试 gate。

Task 038/039/040 的默认测试只验证脚本行为、文件存在性和离线 mock 路径。

## 6. 是否有真实 key 泄露风险

当前仓库内未写入真实 key。

已建立的防护：

- `.env.example` 只使用占位值。
- 不创建 `.env` 或 `.env.local`。
- smoke 脚本输出不打印 API Key。
- 测试检查 `.env.example` 不包含常见 secret 模式，例如 `sk-...`、`Bearer ...`、`AIza...`。
- `.gitignore` 已忽略 `.env`、`.env.*`，但保留 `!.env.example`。

剩余风险主要来自用户本地误操作：把真实图片、`.env.local`、命令输出或日志手动加入版本控制。运行前后应使用 `git status --short` 检查。

## 7. demo_data 是否只包含说明和 `.gitkeep`

是。

当前 demo data 目录只包含：

```text
demo_data/README.md
demo_data/images/.gitkeep
demo_data/videos/.gitkeep
```

仓库未提交真实图片、真实视频或大文件。用户可在本地把低风险图片放入 `demo_data/images/` 后手动运行 smoke。

## 8. 用户下一步如何手动运行真实 Provider

先准备低风险图片：

```text
demo_data/images/shoe.jpg
```

OpenAI 示例：

```bash
export MULTIMODAL_AGENT_VISION_PROVIDER=openai
export OPENAI_API_KEY="<set-in-local-shell>"
export OPENAI_VISION_BASE_URL="https://api.openai.com/v1"
export OPENAI_VISION_MODEL="gpt-4o-mini"
python scripts/smoke_real_vision.py --image demo_data/images/shoe.jpg
```

Qwen 示例：

```bash
export MULTIMODAL_AGENT_VISION_PROVIDER=qwen
export QWEN_API_KEY="<set-in-local-shell>"
export QWEN_VISION_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export QWEN_VISION_MODEL="qwen-vl-plus"
python scripts/smoke_real_vision.py --image demo_data/images/shoe.jpg
```

运行后检查输出：

- `status`
- `provider`
- `intent`
- `response_text`
- `tool_calls`
- `errors`
- `trace_id`

详细 runbook 见：

```text
docs/39-real-provider-smoke-runbook.md
```

## 9. 是否建议进入 Phase 5A

暂不建议直接进入完整 Phase 5。

建议先由用户本地手动运行真实 Vision Provider smoke，并根据结果决定：

- 如果 smoke 成功：可以进入 Phase 5A，优先做真实 Provider response mapping、Trace 查询、成本/超时控制和错误重试。
- 如果出现 `provider_unconfigured`：先修正本地环境变量，不进入 Phase 5。
- 如果出现 `provider_timeout`：先确认网络、base URL、代理和 Provider 可用性。
- 如果出现 `provider_bad_response`：优先开一个小任务修正真实 Provider response parsing，不要扩大到完整 Phase 5。

Phase 5A 应保持小步推进，不应一次性引入鉴权、分布式队列、生产存储和多 Provider 扩展。

## 验收命令

Phase 4.5 smoke review 验收：

```bash
python -m pytest
python scripts/run_evals.py
```
