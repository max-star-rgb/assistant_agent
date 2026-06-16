# 120 Phase 6E：Documentation Consolidation / Release Review

## 目标

把大量 phase docs/tasks 收敛为用户和开发者真正会读的文档，并完成 Phase 6 发布检查。

## 文档收敛目标

最终面向人类用户的文档应集中为：

```text
README.md
docs/quickstart.md
docs/architecture.md
docs/capabilities.md
docs/configuration.md
docs/provider-setup.md
docs/demo-flows.md
docs/deployment-local.md
docs/development.md
docs/security.md
docs/troubleshooting.md
```

Phase docs/tasks 可以保留在：

```text
docs/archive/
tasks/archive/
```

或保持不动，但 README 不应要求普通用户阅读所有 phase 文档。

## 发布检查

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
git status --short
```

## 产物

```text
docs/121-phase6-productization-review.md
```

报告包含：

1. CLI 状态。
2. API/Web console 状态。
3. Real Provider opt-in 状态。
4. Deployment 状态。
5. Documentation 状态。
6. 安全边界。
7. 剩余问题。
8. 下一阶段建议。

## 下一阶段建议

Phase 6 后可考虑：

```text
真实 Provider 深度接入
前端产品化
用户认证
部署到服务器
真实用户试用
```
