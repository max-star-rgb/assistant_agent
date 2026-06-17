# Provider Setup

This project defaults to mock/local providers. Real Provider usage is opt-in and must be configured only in a local shell or an untracked local env file.

Do not commit API keys, authorization headers, raw Provider responses, generated media, real media, or smoke logs.

## Default Offline Setup

No Provider configuration is required for the default demo path:

```bash
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
python scripts/run_demo_flows.py
python -m pytest
```

Default values:

```text
MULTIMODAL_AGENT_RUNTIME_PROFILE=local_demo
MULTIMODAL_AGENT_VISION_PROVIDER=mock
MULTIMODAL_AGENT_CHAT_PROVIDER=mock
MULTIMODAL_AGENT_IMAGE_PROVIDER=mock
MULTIMODAL_AGENT_PRODUCT_PROVIDER=mock
MULTIMODAL_AGENT_PRICE_PROVIDER=mock
MULTIMODAL_AGENT_RENDER_PROVIDER=mock
MULTIMODAL_AGENT_VIDEO_PROVIDER=mock
RUN_INTEGRATION_TESTS=0
```

## Runtime Profile Gate

Real/network Provider selectors only take effect under an explicit runtime profile:

```bash
export MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
```

Use `provider_smoke` for manual smoke checks. `pilot` is reserved for later controlled real usage. In default `local_demo` and `offline_eval`, real/network Provider selectors are ignored by `ProviderConfig.from_env()` so CLI, API, Web Console, tests, evals, and demo flows stay offline.

Setting an API key alone never enables a real Provider. A manual smoke run needs both:

```text
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
MULTIMODAL_AGENT_<CAPABILITY>_PROVIDER=<explicit-provider>
```

## Vision Provider

Supported opt-in providers:

- `openai`
- `qwen`
- `seed`

Environment variables:

```text
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
MULTIMODAL_AGENT_VISION_PROVIDER=openai|qwen|seed
OPENAI_API_KEY
OPENAI_VISION_BASE_URL
OPENAI_VISION_MODEL
QWEN_API_KEY
QWEN_VISION_BASE_URL
QWEN_VISION_MODEL
SEED_API_KEY
SEED_VISION_BASE_URL
SEED_VISION_MODEL
```

Smoke command:

```bash
python scripts/smoke_real_vision.py --image <local-image-path>
```

Missing configuration behavior:

- Missing key or base URL returns a structured `provider_unconfigured` style message.
- The script must not fall back to mock when a real provider is explicitly selected.

## Chat Provider

Supported opt-in providers:

- `openai`
- `qwen`
- `deepseek`
- `local`

Environment variables:

```text
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
MULTIMODAL_AGENT_CHAT_PROVIDER=openai|qwen|deepseek|local
OPENAI_API_KEY
QWEN_API_KEY
DEEPSEEK_API_KEY
DEEPSEEK_CHAT_BASE_URL
DEEPSEEK_CHAT_MODEL
LOCAL_CHAT_BASE_URL
```

Smoke command:

```bash
python scripts/smoke_direct_chat.py --text "用一句话介绍这个项目"
```

Missing configuration behavior:

- Missing key or local base URL prints a clear setup message and exits without calling a real Provider.

## Image Generation Provider

Supported opt-in providers:

- `openai`
- `qwen`
- `comfyui`
- `local`

Environment variables:

```text
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
MULTIMODAL_AGENT_IMAGE_PROVIDER=openai|qwen|comfyui|local
OPENAI_API_KEY
DASHSCOPE_API_KEY
QWEN_IMAGE_BASE_URL
QWEN_IMAGE_MODEL
QWEN_IMAGE_DEFAULT_SIZE
COMFYUI_BASE_URL
LOCAL_IMAGE_BASE_URL
```

Smoke command:

```bash
python scripts/smoke_text_image_generation.py --prompt "生成一张日系极简商品海报"
```

Missing configuration behavior:

- Missing key or base URL prints a clear setup message.
- No generated image should be committed.

## Product Search Provider

Supported providers:

- `mock`
- `local_json`
- `http`

Environment variables:

```text
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
MULTIMODAL_AGENT_PRODUCT_PROVIDER=mock|local_json|http
PRODUCT_SEARCH_LOCAL_PATH
PRODUCT_SEARCH_BASE_URL
PRODUCT_SEARCH_API_KEY
```

Smoke command:

```bash
python scripts/smoke_product_search.py --query "白色运动鞋"
```

Missing configuration behavior:

- `local_json` requires `PRODUCT_SEARCH_LOCAL_PATH`.
- `http` requires `PRODUCT_SEARCH_BASE_URL` and `PRODUCT_SEARCH_API_KEY`.
- Missing values print a clear setup message.

## Price Compare Provider

Supported providers:

- `mock`
- `local`
- `http`

Environment variables:

```text
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
MULTIMODAL_AGENT_PRICE_PROVIDER=mock|local|http
PRICE_COMPARE_BASE_URL
PRICE_COMPARE_API_KEY
```

Smoke command:

```bash
python scripts/smoke_price_compare.py --query "白色运动鞋"
```

Missing configuration behavior:

- `http` requires `PRICE_COMPARE_BASE_URL` and `PRICE_COMPARE_API_KEY`.
- Missing values print a clear setup message.

## Render Provider

Supported opt-in providers:

- `http`

Environment variables:

```text
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
MULTIMODAL_AGENT_RENDER_PROVIDER=mock|http
RENDER_BASE_URL
RENDER_API_KEY
RENDER_TIMEOUT_SECONDS
```

Smoke command:

```bash
python scripts/smoke_render_3d.py --text "把一双白色运动鞋放到展厅中"
```

Missing configuration behavior:

- `http` requires `RENDER_BASE_URL` and `RENDER_API_KEY`.
- Missing values print a clear setup message.

## Video Understanding Provider

Supported opt-in providers:

- `http`

Environment variables:

```text
MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
MULTIMODAL_AGENT_VIDEO_PROVIDER=mock|http
VIDEO_UNDERSTANDING_BASE_URL
VIDEO_UNDERSTANDING_API_KEY
VIDEO_UNDERSTANDING_MODEL
```

Smoke command:

```bash
python scripts/smoke_video_understanding.py --video-ref demo_video_product_1
```

Missing configuration behavior:

- `http` requires `VIDEO_UNDERSTANDING_BASE_URL` and `VIDEO_UNDERSTANDING_API_KEY`.
- Missing values print a clear setup message.

## Integration Test Gate

Real Provider integration tests must remain disabled unless explicitly enabled:

```text
RUN_INTEGRATION_TESTS=1
```

Do not enable this in committed files.
