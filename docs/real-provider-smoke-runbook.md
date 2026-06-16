# Real Provider Smoke Runbook

This runbook documents opt-in smoke paths only. Do not run real Provider smoke commands unless you intentionally configured local environment variables.

## Safety Checklist

Before any real Provider smoke:

- Confirm default `python -m pytest` passes without real Provider calls.
- Set Provider variables only in your local shell or an untracked local env file.
- Do not commit API keys.
- Do not commit raw Provider responses.
- Do not commit generated images, rendered files, videos, or smoke logs.
- Keep `RUN_INTEGRATION_TESTS=0` unless manually running gated integration tests.

## Vision Smoke

```bash
export MULTIMODAL_AGENT_VISION_PROVIDER=qwen
export QWEN_API_KEY="<set-in-local-shell>"
export QWEN_VISION_BASE_URL="<openai-compatible-base-url-ending-with-v1>"
export QWEN_VISION_MODEL="<vision-model-name>"
python scripts/smoke_real_vision.py --image <local-image-path>
```

Expected behavior:

- With full config, calls the selected Provider.
- Without required config, returns a clear unconfigured message.
- Does not silently fall back to mock for explicit real Provider selection.

## Chat Smoke

```bash
export MULTIMODAL_AGENT_CHAT_PROVIDER=qwen
export QWEN_API_KEY="<set-in-local-shell>"
python scripts/smoke_direct_chat.py --text "用一句话介绍这个项目"
```

Expected behavior:

- Missing key or local base URL exits with a clear setup message.

## Image Generation Smoke

```bash
export MULTIMODAL_AGENT_IMAGE_PROVIDER=qwen
export QWEN_API_KEY="<set-in-local-shell>"
python scripts/smoke_text_image_generation.py --text "生成一张日系极简商品海报"
```

Expected behavior:

- Missing key or base URL exits with a clear setup message.
- Do not commit generated outputs.

## Product Search Smoke

Offline local JSON smoke:

```bash
export MULTIMODAL_AGENT_PRODUCT_PROVIDER=local_json
export PRODUCT_SEARCH_LOCAL_PATH=demo_data/products/products.example.json
python scripts/smoke_product_search.py --query "白色运动鞋"
```

HTTP provider smoke:

```bash
export MULTIMODAL_AGENT_PRODUCT_PROVIDER=http
export PRODUCT_SEARCH_BASE_URL="<private-product-search-service>"
export PRODUCT_SEARCH_API_KEY="<set-in-local-shell>"
python scripts/smoke_product_search.py --query "白色运动鞋"
```

Expected behavior:

- Missing local path, base URL, or key exits with a clear setup message.

## Price Compare Smoke

```bash
export MULTIMODAL_AGENT_PRICE_PROVIDER=http
export PRICE_COMPARE_BASE_URL="<private-price-compare-service>"
export PRICE_COMPARE_API_KEY="<set-in-local-shell>"
python scripts/smoke_price_compare.py --query "白色运动鞋"
```

Expected behavior:

- Missing base URL or key exits with a clear setup message.

## Render Smoke

```bash
export MULTIMODAL_AGENT_RENDER_PROVIDER=http
export RENDER_BASE_URL="<private-render-service>"
export RENDER_API_KEY="<set-in-local-shell>"
python scripts/smoke_render_3d.py --text "把一双白色运动鞋放到展厅中"
```

Expected behavior:

- Missing base URL or key exits with a clear setup message.
- Do not commit rendered outputs.

## Video Understanding Smoke

```bash
export MULTIMODAL_AGENT_VIDEO_PROVIDER=http
export VIDEO_UNDERSTANDING_BASE_URL="<private-video-understanding-service>"
export VIDEO_UNDERSTANDING_API_KEY="<set-in-local-shell>"
export VIDEO_UNDERSTANDING_MODEL="<optional-model-name>"
python scripts/smoke_video_understanding.py --video-ref demo_video_product_1
```

Expected behavior:

- Missing base URL or key exits with a clear setup message.
- Do not commit real videos, frames, raw responses, or logs.

## Default Regression Commands

These commands must remain offline:

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
```
