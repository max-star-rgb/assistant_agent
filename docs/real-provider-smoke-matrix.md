# Real Provider Smoke Matrix

This matrix documents opt-in smoke paths. Real Provider smoke is never default-enabled.

| provider | capability | status | required_env | smoke_script | default_enabled | notes |
| --- | --- | --- | --- | --- | --- | --- |
| mock | direct_chat | supported | none | `python scripts/smoke_direct_chat.py --text "用一句话介绍这个项目"` | false | Default runtime uses mock without real calls. |
| openai | direct_chat | opt-in skeleton | `MULTIMODAL_AGENT_CHAT_PROVIDER=openai`, `OPENAI_API_KEY` | `python scripts/smoke_direct_chat.py --text "用一句话介绍这个项目"` | false | Requires local shell config; missing key exits with setup message. |
| qwen | direct_chat | opt-in skeleton | `MULTIMODAL_AGENT_CHAT_PROVIDER=qwen`, `QWEN_API_KEY` | `python scripts/smoke_direct_chat.py --text "用一句话介绍这个项目"` | false | Requires local shell config; missing key exits with setup message. |
| local | direct_chat | opt-in skeleton | `MULTIMODAL_AGENT_CHAT_PROVIDER=local`, `LOCAL_CHAT_BASE_URL` | `python scripts/smoke_direct_chat.py --text "用一句话介绍这个项目"` | false | For private local OpenAI-compatible service. |
| openai | image_understanding | opt-in supported | `MULTIMODAL_AGENT_VISION_PROVIDER=openai`, `OPENAI_API_KEY`, optional `OPENAI_VISION_BASE_URL`, optional `OPENAI_VISION_MODEL` | `python scripts/smoke_real_vision.py --image <local-image-path>` | false | Does not fall back to mock when explicitly selected. |
| qwen | image_understanding | opt-in supported | `MULTIMODAL_AGENT_VISION_PROVIDER=qwen`, `QWEN_API_KEY`, optional `QWEN_VISION_BASE_URL`, optional `QWEN_VISION_MODEL` | `python scripts/smoke_real_vision.py --image <local-image-path>` | false | User previously validated this manually; no outputs are committed. |
| seed | image_understanding | opt-in supported | `MULTIMODAL_AGENT_VISION_PROVIDER=seed`, `SEED_API_KEY`, `SEED_VISION_BASE_URL`, optional `SEED_VISION_MODEL` | `python scripts/smoke_real_vision.py --image <local-image-path>` | false | Requires OpenAI-compatible base URL. |
| openai | image_generation | opt-in skeleton | `MULTIMODAL_AGENT_IMAGE_PROVIDER=openai`, `OPENAI_API_KEY` | `python scripts/smoke_text_image_generation.py --text "生成一张日系极简商品海报"` | false | Do not commit generated images. |
| qwen | image_generation | opt-in supported | `MULTIMODAL_AGENT_IMAGE_PROVIDER=qwen`, `DASHSCOPE_API_KEY`, optional `QWEN_IMAGE_BASE_URL`, optional `QWEN_IMAGE_MODEL` | `python scripts/smoke_text_image_generation.py --prompt "生成一张日系极简商品海报"` | false | Do not commit generated images. |
| ark | image_generation | opt-in supported | `MULTIMODAL_AGENT_IMAGE_PROVIDER=ark`, `ARK_API_KEY`, `ARK_IMAGE_BASE_URL`, `ARK_IMAGE_MODEL` | `python scripts/demo_assistant_loop.py "生成一张日系极简商品海报"` | false | CLI demo follows `MULTIMODAL_AGENT_IMAGE_PROVIDER`. Do not commit generated images. |
| comfyui | image_generation | opt-in skeleton | `MULTIMODAL_AGENT_IMAGE_PROVIDER=comfyui`, `COMFYUI_BASE_URL` | `python scripts/smoke_text_image_generation.py --text "生成一张日系极简商品海报"` | false | Intended for local/private ComfyUI service. |
| local | image_generation | opt-in skeleton | `MULTIMODAL_AGENT_IMAGE_PROVIDER=local`, `LOCAL_IMAGE_BASE_URL` | `python scripts/smoke_text_image_generation.py --text "生成一张日系极简商品海报"` | false | Intended for local/private image service. |
| local_json | product_search | offline supported | `MULTIMODAL_AGENT_PRODUCT_PROVIDER=local_json`, `PRODUCT_SEARCH_LOCAL_PATH` | `python scripts/smoke_product_search.py --query "白色运动鞋"` | false | Offline local JSON path; no network. |
| http | product_search | opt-in skeleton | `MULTIMODAL_AGENT_PRODUCT_PROVIDER=http`, `PRODUCT_SEARCH_BASE_URL`, `PRODUCT_SEARCH_API_KEY` | `python scripts/smoke_product_search.py --query "白色运动鞋"` | false | Private HTTP provider only; no crawler/login/payment. |
| local | price_compare | offline supported | `MULTIMODAL_AGENT_PRICE_PROVIDER=local` | `python scripts/smoke_price_compare.py --query "白色运动鞋"` | false | Offline/local comparison path. |
| http | price_compare | opt-in skeleton | `MULTIMODAL_AGENT_PRICE_PROVIDER=http`, `PRICE_COMPARE_BASE_URL`, `PRICE_COMPARE_API_KEY` | `python scripts/smoke_price_compare.py --query "白色运动鞋"` | false | Private HTTP provider only; no crawler/login/payment. |
| http | render_3d | opt-in skeleton | `MULTIMODAL_AGENT_RENDER_PROVIDER=http`, `RENDER_BASE_URL`, `RENDER_API_KEY`, optional `RENDER_TIMEOUT_SECONDS` | `python scripts/smoke_render_3d.py --text "把一双白色运动鞋放到展厅中"` | false | Do not commit rendered outputs. |
| http | video_understanding | opt-in skeleton | `MULTIMODAL_AGENT_VIDEO_PROVIDER=http`, `VIDEO_UNDERSTANDING_BASE_URL`, `VIDEO_UNDERSTANDING_API_KEY`, optional `VIDEO_UNDERSTANDING_MODEL` | `python scripts/smoke_video_understanding.py --video-ref demo_video_product_1` | false | Do not commit real videos, frames, raw responses, or logs. |

## Default Regression Rule

These commands must not call real Providers:

```bash
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
```

## Deferred Items

- Public Provider certification is deferred.
- Production credentials management is deferred.
- Remote MCP publishing is deferred.
- Production frontend upload flows are deferred.
