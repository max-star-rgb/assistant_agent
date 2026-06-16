# Security

## Defaults

- Real Providers are default-off.
- Integration tests are default-off.
- Mock/local providers are the default demo path.

## Secrets

Do not commit:

- API keys
- Authorization headers
- `Bearer` tokens
- real `.env` files
- raw Provider responses
- full base64 payloads
- sensitive local paths

Use `.env.example` only for variable names and placeholders.

## Redaction

API errors, trace summaries, and public debug outputs should be redacted before exposure.

## Provider Opt-in

Real Provider setup is documented in:

```text
docs/provider-setup.md
docs/real-provider-smoke-runbook.md
docs/real-provider-smoke-matrix.md
```

Real Provider smoke is manual and must not be triggered by default pytest, evals, demo runner, CLI, API, Web Console, MCP smoke, or skills validation.
