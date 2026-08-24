# Supervisor Todo Planning A-lite Production TDD

- Scope: production planning graph RED/GREEN tests
- Provider mode: mock/offline only
- Temporary tests: user may delete this whole directory manually
- Legacy note: `native-high-agency-planner` and `planning-recovery-routing` protect the retired design and remain user-owned temporary directories.

## Command

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/supervisor-todo-planning-alite-production
```
