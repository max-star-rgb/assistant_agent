# 130 Phase 7E Deployment Readiness

## Goal

Make the service deployable on a single small server.

## Scope

- Verify Docker build.
- Verify docker-compose.
- Add production-like env template without secrets.
- Document persistent local paths.
- Add process runbook.
- Add backup/restore notes.
- Add healthcheck/readiness checks.

## Out of Scope

- No Kubernetes.
- No distributed queue.
- No production observability stack unless required.

## Success Criteria

- Local server deployment is documented.
- `.env.example` is complete and secret-free.
- health/readiness checks are documented.
- local state paths are documented.
