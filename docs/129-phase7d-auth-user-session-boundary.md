# 129 Phase 7D Auth / User / Session Boundary

## Goal

Introduce a minimal user/session boundary for pilot safety.

## Scope

- Define local/pilot auth mode.
- Add user/session ownership checks for run and trace queries.
- Add memory ownership checks.
- Add simple pilot token support if needed.
- Keep secrets out of the repo.

## Out of Scope

- No enterprise IAM.
- No OAuth unless explicitly required.
- No billing or account system.

## Success Criteria

- `local_demo` remains easy to use.
- `pilot` can require a token.
- run/trace/memory queries cannot cross users.
- errors remain redacted.
