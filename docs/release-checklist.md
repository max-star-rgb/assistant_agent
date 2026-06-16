# Release Checklist

Use this checklist before handing off the local demo.

## Required Commands

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
git status --short
```

## Documentation

- README points users to consolidated docs.
- `docs/quickstart.md` exists.
- `docs/architecture.md` exists.
- `docs/capabilities.md` exists.
- `docs/configuration.md` exists.
- `docs/provider-setup.md` exists.
- `docs/demo-flows.md` exists.
- `docs/deployment-local.md` exists.
- `docs/development.md` exists.
- `docs/security.md` exists.
- `docs/troubleshooting.md` exists.

## Safety

- `.env.example` contains placeholders only.
- No `.env` or `.env.local` is committed.
- No real API key is committed.
- No real media is committed.
- No generated image is committed.
- No rendered artifact is committed.
- No raw Provider response is committed.
- Default Provider selectors remain mock/local.
- `RUN_INTEGRATION_TESTS=0` by default.

## Cleanup

- Remove `__pycache__/`.
- Remove `.pytest_cache/`.
- Remove `.mypy_cache/`.
- Remove `.ruff_cache/`.
- Do not remove phase docs unless explicitly requested.

## Known Local Limitation

Docker build cannot be verified on machines without Docker installed. In that case, record `docker: command not found` and use the local Python run path.
