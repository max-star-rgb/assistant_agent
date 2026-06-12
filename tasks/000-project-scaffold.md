# Task 000：项目骨架

## Goal

创建 Python 项目最小骨架，为后续 Agent 开发提供可运行、可测试的基础。

## Read first

- `AGENTS.md`
- `docs/00-doc-map.md`
- `docs/02-repository-layout.md`
- `docs/08-testing.md`

## Scope

允许新增：

```text
pyproject.toml
src/multimodal_agent/
tests/
README.md
```

## Steps

1. 创建 `src/multimodal_agent/` 包。
2. 创建基础模块目录：`api/ agent/ schemas/ tools/ memory/ services/ utils/`。
3. 创建 `pyproject.toml`，至少包含 pytest 配置。
4. 创建 `tests/unit/test_imports.py`，验证包可导入。
5. 在 README 中写明本项目目标和本地测试命令。

## Acceptance

```bash
pytest
```

必须通过。

## Out of scope

- 不实现 Agent 逻辑。
- 不接入任何真实外部 API。
- 不添加数据库服务。
