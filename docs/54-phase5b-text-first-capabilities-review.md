# 54 Phase 5B Review：Text-first Capabilities

## 审计结论

Phase 5B 已完成。当前系统已经把 `direct_chat` 和 `image_generation` 从 routing-only 推进到可测试、可替换 Provider、默认离线的 text-first capabilities。

默认路径仍使用 Mock/Local adapter，不调用真实外部 Provider，不需要图片或视频输入。

## 1. Direct Chat 状态

已完成：

- `direct_chat` 支持纯文本输入。
- 默认运行时 `AgentGraphRuntime` 可在无工具调用时调用 `ChatAdapter`。
- `MockChatAdapter` 返回确定性结构化结果。
- 选择 `openai`、`qwen` 或 `local` 但缺少配置时返回 `provider_unconfigured`，不回退伪装成真实结果。
- API 对 direct_chat 返回稳定 `contract`，且 `tool_calls` / `tool_results` 为空。

关键文件：

- `src/multimodal_agent/services/chat_adapter.py`
- `src/multimodal_agent/agent/runtime.py`
- `src/multimodal_agent/agent/prompt_builder.py`
- `tests/test_direct_chat_adapter.py`
- `tests/test_direct_chat_routing.py`
- `tests/test_text_capability_api.py`

## 2. Image Generation 状态

已完成：

- `image_generation` 支持纯文本输入，不要求图片或视频。
- `ImageGenerationTool` 通过 `ImageGenerationAdapter` 执行生成能力。
- `MockImageGenerationAdapter` 返回确定性 `local://generated/poster.png`。
- 选择真实 image provider 但缺少配置时返回 `provider_unconfigured`。
- 真实生成物目录使用 `.local/generated/`，并已被 `.gitignore` 忽略。

关键文件：

- `src/multimodal_agent/services/image_generation_adapter.py`
- `src/multimodal_agent/tools/image_generation_tool.py`
- `src/multimodal_agent/agent/tool_input_builder.py`
- `tests/test_image_generation_adapter.py`
- `tests/test_text_only_image_generation.py`
- `tests/test_text_capability_api.py`

## 3. Prompt / Output Contract

已完成：

- `prompt_builder.py` 集中构造 direct_chat 和 image_generation 请求。
- Prompt 构造保持 provider-neutral，不写 provider-specific payload。
- 文本能力输出通过 `contract` 暴露：
  - `capability`
  - `status`
  - `output_ref`
  - `data`
  - `errors`
- API 不暴露 provider raw response。

关键测试：

- `tests/test_prompt_builder.py`
- `tests/test_text_capability_output_contracts.py`
- `tests/test_text_capability_api.py`

## 4. Mock 与真实 Provider 边界

当前边界清晰：

- 默认 `ProviderConfig` 使用 `mock`。
- 默认 `pytest` 和 eval 不触发真实 Provider。
- smoke 脚本只有用户显式运行才执行。
- 缺少真实 Provider 配置时返回 `provider_unconfigured`。
- Phase 5B 尚未实现真实 chat/image generation HTTP 调用，只完成可选接入结构和安全边界。

允许后续扩展：

- OpenAI-compatible chat。
- Qwen chat。
- OpenAI / Qwen / ComfyUI / local image generation。

禁止默认行为：

- 默认调用真实外部 Provider。
- 把 mock 输出伪装成真实生成结果。
- 写入或提交 API Key。
- 提交真实生成图片。

## 5. Smoke 能力

已完成两个手动入口：

```bash
python scripts/smoke_direct_chat.py --text "帮我写一段商品介绍"
python scripts/smoke_text_image_generation.py --prompt "生成一张日系极简商品海报"
```

安全状态：

- import 脚本不会触发 Provider。
- 默认 mock 可运行。
- 真实 Provider 缺配置时清晰提示。
- text image smoke 输出只暴露相对生成目录 `.local/generated`，不暴露本机绝对路径。

关键测试：

- `tests/test_text_capability_smoke_scripts.py`

## 6. Eval 覆盖

当前 eval 覆盖已包含：

- direct_chat 纯文本。
- direct_chat 带图片但不触发 vision。
- text-only image_generation。
- text-only image_generation 不要求 image/video。
- 与 Phase 5A baseline 共存的 routing、memory、multistep、media cases。

最近验收结果：

```text
python scripts/run_evals.py
49 passed / 49 total
```

关键文件：

- `tests/evals/eval_cases.json`
- `scripts/run_evals.py`
- `tests/test_text_capability_evals.py`

## 7. Key / Data 泄露风险

当前未发现真实 API Key 写入仓库。

已确认：

- `.env` 和 `.env.*` 被忽略，`.env.example` 只保留占位符。
- `.local/` 和 `.local/generated/` 被忽略。
- 默认测试不调用真实 Provider。
- smoke 缺 key 时只输出缺失变量名，不输出 header 或 token。
- API 输出不包含 Authorization header、Bearer token 或 provider raw response。

剩余注意事项：

- Vision adapter 内部仍会为真实视觉调用构造 base64 data URL；这是 Phase 4.5 真实 Vision smoke 的既有能力。默认测试不调用真实 Provider，后续若扩展真实 image generation，也应避免在 trace、错误、日志中输出完整 base64。

## 8. Phase 5C 建议

建议 Phase 5C 不继续扩展 Vision hardening，而是进入一个明确的业务能力阶段：

```text
Phase 5C Product Search / Price Compare Provider Baseline
```

建议目标：

- 为 `product_search` 定义真实 Provider adapter contract。
- 为 `price_compare` 定义真实 Provider adapter contract。
- 默认继续使用 MockAdapter。
- 增加 env-gated smoke 或 integration test。
- 缺配置返回 `provider_unconfigured`。
- 不默认联网调用真实商品或价格服务。

暂不建议在 Phase 5C 同时做：

- 3D render provider。
- Memory hardening。
- Provider retry/cost hardening。
- 完整 Harness engineering。
