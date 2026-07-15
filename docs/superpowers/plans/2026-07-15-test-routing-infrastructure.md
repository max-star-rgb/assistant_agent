# 测试路由基础设施实施计划

> **执行要求：** 使用 `superpowers:executing-plans` 按任务执行；步骤使用 checkbox 跟踪。

**目标：** 交付可用的 scoped test runner、机器可读 scope map 和 Codex 路由文档，让普通开发运行临时 critical bootstrap 与受影响 scope，同时暂不改变裸 pytest 的现有行为。

**架构：** `tests/scope-map.toml` 声明临时 critical 路径、源码匹配规则和测试路径模式；`scripts/run_scoped_tests.py` 解析配置、展开路径、从 Git range 选择 scope，并在 mock/offline 环境调用 pytest。第一阶段以现有 `tests/unit` 和 `tests/contracts` 作为临时 critical，后续再迁入 `tests/critical` 和 `tests/scopes`。

**技术栈：** Python 3.12、标准库 `argparse` / `dataclasses` / `fnmatch` / `pathlib` / `subprocess` / `tomllib`、pytest 8、Git。

## 全局约束

- 使用 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python`，不安装依赖，不联网，不调用真实 Provider。
- Runner 强制 `MULTIMODAL_AGENT_RUNTIME_PROFILE=mock`、`MULTIMODAL_AGENT_DISABLE_DOTENV=1`，并移除 `RUN_INTEGRATION_TESTS`。
- 第一阶段不修改 `pyproject.toml` 的 `testpaths`；裸 pytest 保持当前行为。
- `tests/README.md` 是人类可读权威，`tests/scope-map.toml` 是机器可读权威，`AGENTS.md` 只保留入口。
- 未映射源码路径和无法展开的测试模式都必须保守失败。
- 规格、计划、代码、测试和文档验证完成后统一创建一个本地 commit，不 push。

---

### 任务 1：Scope Map 数据模型与路径选择

**文件：**
- 新建：`scripts/run_scoped_tests.py`
- 新建：`tests/unit/test_scoped_test_runner.py`

**接口：**
- `ScopeDefinition(name: str, source_paths: tuple[str, ...], test_paths: tuple[str, ...])`
- `ScopeMap(critical_paths: tuple[str, ...], scopes: tuple[ScopeDefinition, ...])`
- `TestSelection(scopes: tuple[str, ...], test_paths: tuple[str, ...])`
- `load_scope_map(path: Path) -> ScopeMap`
- `select_explicit_scopes(scope_map: ScopeMap, names: list[str]) -> TestSelection`
- `select_changed_scopes(scope_map: ScopeMap, changed_paths: list[str]) -> TestSelection`
- `expand_test_paths(repo_root: Path, patterns: tuple[str, ...]) -> tuple[str, ...]`

- [x] **步骤 1：写失败测试**

```python
def test_load_scope_map_and_select_explicit_scope(tmp_path: Path) -> None:
    config = tmp_path / "scope-map.toml"
    config.write_text(
        '[critical]\ntest_paths=["tests/unit"]\n'
        '[[scope]]\nname="tools"\n'
        'source_paths=["src/assistant_agent/tools/**"]\n'
        'test_paths=["tests/test_tool_*.py"]\n',
        encoding="utf-8",
    )
    selection = MODULE.select_explicit_scopes(MODULE.load_scope_map(config), ["tools"])
    assert selection.scopes == ("tools",)
    assert selection.test_paths == ("tests/unit", "tests/test_tool_*.py")


def test_unmapped_source_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="未映射源码路径"):
        MODULE.select_changed_scopes(
            scope_map(),
            ["src/assistant_agent/unknown/new_boundary.py"],
        )


def test_expand_test_paths_rejects_empty_pattern(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="没有匹配测试"):
        MODULE.expand_test_paths(tmp_path, ("tests/missing_*.py",))
```

- [x] **步骤 2：运行 RED**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/unit/test_scoped_test_runner.py -q
```

预期：因 runner 文件或接口不存在而失败。

- [x] **步骤 3：实现最小解析与选择**

使用冻结 dataclass。`load_scope_map` 拒绝缺失 `[critical]`、重名 scope、空 `source_paths` 和空 `test_paths`；显式 scope 稳定去重，未知名称抛出 `ValueError`。源码匹配使用 `fnmatch.fnmatchcase`。`tests/**`、runner、scope map、conftest 和 `pyproject.toml` 视为共享测试基础设施并选择所有 scope；普通 docs 只运行 critical。

- [x] **步骤 4：运行 GREEN**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/unit/test_scoped_test_runner.py -q
```

---

### 任务 2：CLI、Git Range 与 Pytest 执行

**文件：**
- 修改：`scripts/run_scoped_tests.py`
- 修改：`tests/unit/test_scoped_test_runner.py`

**接口：**
- `changed_paths_for_range(repo_root: Path, git_range: str) -> tuple[str, ...]`
- `build_pytest_command(python: str, test_paths: tuple[str, ...], extra_args: list[str]) -> list[str]`
- `offline_environment(base: Mapping[str, str]) -> dict[str, str]`
- `main(argv: list[str] | None = None) -> int`

- [x] **步骤 1：写命令与环境失败测试**

```python
def test_build_pytest_command_uses_selected_paths() -> None:
    assert MODULE.build_pytest_command(
        "/env/bin/python",
        ("tests/unit", "tests/test_gateway.py"),
        ["-q"],
    ) == [
        "/env/bin/python", "-m", "pytest",
        "tests/unit", "tests/test_gateway.py", "-q",
    ]


def test_offline_environment_removes_integration_opt_in() -> None:
    env = MODULE.offline_environment({"RUN_INTEGRATION_TESTS": "1"})
    assert "RUN_INTEGRATION_TESTS" not in env
    assert env["MULTIMODAL_AGENT_RUNTIME_PROFILE"] == "mock"
    assert env["MULTIMODAL_AGENT_DISABLE_DOTENV"] == "1"
```

- [x] **步骤 2：运行 RED 后实现 CLI**

互斥入口为可重复 `--scope NAME`、`--changed BASE..HEAD`、`--full-legacy`。`--` 后参数原样传给 pytest。普通模式运行 critical 与选中 scope；full legacy 固定运行 `python -m pytest tests -m "not integration"`。执行前打印 mode、scopes、test paths，并原样传播 pytest 退出码。

- [x] **步骤 3：写 Git 与退出码测试**

```python
def test_changed_paths_for_range_reports_invalid_range(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    with pytest.raises(ValueError, match="非法 Git range"):
        MODULE.changed_paths_for_range(tmp_path, "missing..HEAD")


def test_main_propagates_pytest_exit_code(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["pytest"], 5),
    )
    assert MODULE.main([
        "--repo-root", str(tmp_path),
        "--scope-map", "scope-map.toml",
        "--scope", "tools",
    ]) == 5
```

临时 Git 测试还要覆盖 add/modify/delete/rename、多 scope 选择、非法 range，以及 runner 前后 tracked files 不变。

- [x] **步骤 4：实现 Git range 与 subprocess 执行并跑 GREEN**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/unit/test_scoped_test_runner.py -q
```

---

### 任务 3：真实 Scope Map 与 Bootstrap 入口

**文件：**
- 新建：`tests/scope-map.toml`
- 新建：`tests/critical/README.md`
- 新建：`tests/scopes/README.md`
- 修改：`tests/unit/test_scoped_test_runner.py`

**接口：**
- 临时 critical 固定为 `tests/unit` 与 `tests/contracts`。
- scope 固定为 `prompt`、`context`、`tools`、`gateway`、`runtime`、`memory`、`providers`、`api`。

- [x] **步骤 1：写真实配置失败测试**

```python
def test_repository_scope_map_has_required_scopes() -> None:
    scope_map = MODULE.load_scope_map(PROJECT_ROOT / "tests/scope-map.toml")
    assert scope_map.critical_paths == ("tests/unit", "tests/contracts")
    assert {scope.name for scope in scope_map.scopes} == {
        "prompt", "context", "tools", "gateway",
        "runtime", "memory", "providers", "api",
    }
```

- [x] **步骤 2：创建真实配置**

源码模式覆盖：prompt builder/system policy；`services/context/**`；tools/validator/executor/MCP；gateway/realtime；agent/runtime/routing；memory/tools/services/schemas；providers/provider services/config/runtime profile；api/identity/trial access。测试路径使用明确 glob，runner 展开并稳定排序。

- [x] **步骤 3：新增 bootstrap 说明**

`tests/critical/README.md` 说明当前 bootstrap 尚未切换裸 pytest。`tests/scopes/README.md` 列出八个 scope、迁移顺序，以及“迁移时移动或重写并删除旧副本，不复制形成双份权威”的规则。

- [x] **步骤 4：验证真实选择与 bootstrap**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py \
  --scope tools -- --collect-only -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/unit tests/contracts -q
```

---

### 任务 4：文档与 Codex 路由

**文件：**
- 修改：`tests/README.md`
- 修改：`AGENTS.md`
- 修改：`.codex/skills/assistant-agent-test-governance/SKILL.md`
- 修改：`tests/unit/test_scoped_test_runner.py`

- [x] **步骤 1：写文档路由失败测试**

```python
def test_test_documentation_routes_scoped_and_legacy_commands() -> None:
    readme = (PROJECT_ROOT / "tests/README.md").read_text(encoding="utf-8")
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    skill = (PROJECT_ROOT / ".codex/skills/assistant-agent-test-governance/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "run_scoped_tests.py --changed" in readme
    assert "run_scoped_tests.py --full-legacy" in readme
    assert "普通开发" in agents and "run_scoped_tests.py" in agents
    assert "full-legacy" in skill
```

- [x] **步骤 2：确认 RED 后更新文档**

`tests/README.md` 记录结构、scope、layer、bootstrap 状态、命令、full legacy 触发条件和新增测试门槛。`AGENTS.md` 只增加简短入口。治理 Skill 改为普通局部治理运行 critical + affected scopes；仅跨 scope、发布门槛、共享测试基础设施变更或用户显式要求时运行 full legacy。

- [x] **步骤 3：验证文档与 Skill**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/unit/test_scoped_test_runner.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  /home/lenovo1/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/assistant-agent-test-governance
git diff --check
```

---

### 任务 5：阶段验收与统一提交

**文件：** 验证并提交本计划涉及的全部文件。

- [x] **步骤 1：运行 runner 自测和 bootstrap**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/unit/test_scoped_test_runner.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py \
  --scope tools -- -q
```

- [x] **步骤 2：验证 full legacy collect-only、fast、collector 与 Skill**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py \
  --full-legacy -- --collect-only -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  .codex/skills/assistant-agent-test-governance/scripts/collect_test_evidence.py \
  --repo-root . --profile none
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  /home/lenovo1/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/assistant-agent-test-governance
git diff --check
```

本阶段不删除旧测试、不切换 pytest 默认入口，因此不自动再次运行十二分钟 full legacy；若 runner 或配置实际改变旧套件收集结果，则升级为完整 full legacy。

- [x] **步骤 3：只暂存本阶段文件并提交**

```bash
git add AGENTS.md .codex/skills/assistant-agent-test-governance/SKILL.md \
  docs/superpowers/specs/2026-07-15-test-suite-rebuild-design.md \
  docs/superpowers/plans/2026-07-15-test-routing-infrastructure.md \
  scripts/run_scoped_tests.py tests/README.md tests/scope-map.toml \
  tests/critical/README.md tests/scopes/README.md \
  tests/unit/test_scoped_test_runner.py
git commit -m "test: add scoped test routing infrastructure"
```

提交前确认没有暂存无关文件，不 push。
