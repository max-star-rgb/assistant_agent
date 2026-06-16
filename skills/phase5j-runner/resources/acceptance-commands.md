# Phase 5J Acceptance Commands

## Task 101

```bash
python -m pytest
```

## Task 102

```bash
python -m pytest
```

## Task 103

```bash
python scripts/smoke_mcp_tools.py
python -m pytest
```

## Task 104

```bash
python scripts/validate_skills.py
python -m pytest
```

## Task 105

```bash
python scripts/validate_skills.py
python -m pytest
```

## Task 106

```bash
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
python -m pytest
```

Optional if implemented:

```bash
python scripts/run_evals.py --suite packaging
```

## Task 107 final review

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
git status --short
```
