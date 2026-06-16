# Phase 5I Acceptance Commands

## Default after most tasks

```bash
python -m pytest
python scripts/run_evals.py
```

## Task 099

```bash
python scripts/run_evals.py
python scripts/run_evals.py --suite memory
python scripts/run_demo_flows.py
python -m pytest
```

## Task 100 final review

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_evals.py --suite memory
python scripts/run_demo_flows.py
git status --short
```
