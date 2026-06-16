# 123 Phase 6C Real Provider Opt-in Review

## Conclusion

Phase 6C Real Provider Opt-in Demo is complete. The project now documents how to opt in to real Provider smoke runs without changing the default mock/local/offline behavior.

## 1. Provider Setup Documentation Status

Provider setup is documented in:

```text
docs/provider-setup.md
```

It covers:

- Vision Provider
- Chat Provider
- Image Generation Provider
- Product Search Provider
- Price Compare Provider
- Render Provider
- Video Understanding Provider

For each Provider family, the document lists:

- supported opt-in provider values
- required environment variables
- smoke command
- missing configuration behavior
- default offline boundary

## 2. Smoke Matrix Status

The smoke matrix is documented in:

```text
docs/real-provider-smoke-matrix.md
```

The matrix includes:

- `provider`
- `capability`
- `status`
- `required_env`
- `smoke_script`
- `default_enabled`
- `notes`

Every documented real Provider smoke path has:

```text
default_enabled = false
```

The matrix also documents deferred items such as public Provider certification, production credentials management, and remote MCP publishing.

## 3. Default Mock / Local Boundary

Phase 6C does not change the default runtime path.

These commands remain offline by default:

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
```

Default Provider values remain mock/local:

```text
MULTIMODAL_AGENT_VISION_PROVIDER=mock
MULTIMODAL_AGENT_CHAT_PROVIDER=mock
MULTIMODAL_AGENT_IMAGE_PROVIDER=mock
MULTIMODAL_AGENT_PRODUCT_PROVIDER=mock
MULTIMODAL_AGENT_PRICE_PROVIDER=mock
MULTIMODAL_AGENT_RENDER_PROVIDER=mock
MULTIMODAL_AGENT_VIDEO_PROVIDER=mock
RUN_INTEGRATION_TESTS=0
```

## 4. API Key Safety Status

Phase 6C did not add real API keys.

Safety status:

- `.env.example` contains only placeholders.
- No `.env` or `.env.local` file was created.
- Real Provider smoke commands require users to set variables locally.
- Smoke outputs, raw Provider responses, real media, generated images, rendered files, and logs must not be committed.
- Missing keys or base URLs should produce clear setup messages instead of silent mock fallback for explicitly selected real Providers.

## 5. Remaining Issues

- Phase 6C documents opt-in paths; it does not certify every real Provider.
- Production secret management is not implemented.
- Real Provider smoke execution remains manual.
- Product search and price compare HTTP Providers remain private-service skeleton paths, not crawlers or commerce integrations.
- Web Console still defaults to mock/local and does not expose Provider setup controls.

## 6. Phase 6D Recommendation

Proceed to Phase 6D: Local Deployment / Config / Observability.

Recommended next work:

- Add local deployment and configuration runbooks.
- Add or document healthcheck and observability commands.
- Keep default deployment mock/local/offline.
- Do not add Kubernetes or production permission systems in Phase 6D.
