# 52 Text Capability Smoke and Safety

## 目标

为 direct_chat 和 image_generation 提供用户手动 smoke 入口，但默认不调用真实 Provider。

## Smoke 脚本建议

```text
scripts/smoke_direct_chat.py
scripts/smoke_text_image_generation.py
```

## Direct Chat Smoke

示例：

```bash
python scripts/smoke_direct_chat.py --text "帮我写一段商品介绍"
```

默认可使用 MockChatAdapter。

如果用户显式启用真实 Provider：

```bash
export MULTIMODAL_AGENT_CHAT_PROVIDER=qwen
export QWEN_API_KEY="<local-only>"
python scripts/smoke_direct_chat.py --text "帮我写一段商品介绍"
```

## Image Generation Smoke

示例：

```bash
python scripts/smoke_text_image_generation.py --prompt "生成一张日系极简商品海报"
```

默认应使用 MockImageGenerationAdapter。

如果启用真实 Provider，生成物应写入：

```text
.local/generated/
```

该目录必须被 `.gitignore` 忽略。

## 安全要求

- 不写 API Key。
- 不提交真实生成图片。
- 不输出 Authorization header。
- 不输出完整 provider raw response。
- 不默认批量生成。
- 默认 pytest 不调用真实 Provider。

## 验收标准

- smoke 脚本 import 不触发 provider。
- 缺 key 时清晰提示。
- 默认 mock smoke 可运行。
- 真实 provider 只能用户手动触发。
