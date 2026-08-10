# Agent-first Documentation Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立无需新增依赖的文档 authority manifest 与离线 validator，并以 runtime observability 和 Agent eval 两个领域证明 Agent-first 路由与防复制机制可用。

**Architecture:** `docs/authority.toml` 只保存机器可读的领域 owner、适用源码、薄引用和排他事实；`scripts/check_documentation_authority.py` 使用标准库 `tomllib`、Git 和确定性 Markdown 扫描验证结构并输出 JSON。`AGENTS.md` 负责启动与完成门禁，领域 Markdown 保存正文，现有 documentation evidence collector 继续独立检查库存与链接。

**Tech Stack:** Python 3.11、`tomllib`、`argparse`、`pathlib`、`subprocess`、pytest；不新增依赖。

## Global Constraints

- 默认 mock/local/offline，不读取 `.env`，不联网，不调用真实 Provider。
- 不修改产品 Runtime、Experiment、Tool、Memory 或 Gateway 行为。
- 不修改 `tests/core`；临时 RED/GREEN 只放 `tests/tdd/documentation-authority/`，用户可手动删除。
- 第一阶段只登记 `runtime-observability` 与 `agent-eval`，manifest 使用 `coverage = "pilot"`。
- `docs/development/**`、`docs/superpowers/**`、`docs/interview/**` 不参与当前排他事实扫描。
- 保留当前未提交改动；不通过回滚或整文件覆盖清理工作区。
- 当前工作区已有同文件改动，不做中间提交；完成后按仓库规则报告并由用户决定提交边界。

---

### Task 1: Manifest parser 与结构化报告

**Files:**
- Create: `docs/authority.toml`
- Create: `scripts/check_documentation_authority.py`
- Create: `tests/tdd/documentation-authority/test_documentation_authority.py`

**Interfaces:**
- Consumes: Python 3.11 `tomllib` 与仓库根路径。
- Produces: `AuthorityManifest.load(repo_root: Path, manifest_path: Path | None = None) -> AuthorityManifest`、`validate_repository(repo_root: Path, *, git_range: str | None = None) -> ValidationReport`、`main(argv: list[str] | None = None) -> int`。
- Produces: CLI JSON `{schema_version, valid, errors, review_required}`；合法时退出 0，结构错误时退出 2。

- [x] **Step 1: 写 manifest 解析失败测试**

在临时 Git 仓库创建最小 authority 和 TOML，覆盖合法解析、未知 schema、重复 domain ID、缺失字段、错误字段类型与仓库外路径。测试直接导入 `from scripts import check_documentation_authority as authority`，核心断言示例：

```python
report = authority.validate_repository(repo)
assert report.valid is False
assert {item.code for item in report.errors} == {"duplicate_domain_id"}
```

- [x] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/documentation-authority/test_documentation_authority.py -k manifest
```

Expected: collection fails because `scripts.check_documentation_authority` does not exist.

- [x] **Step 3: 实现最小 parser 和 report model**

只使用 frozen dataclass，定义：

```python
@dataclass(frozen=True)
class AuthorityDomain:
    id: str
    authority: str
    read_when: tuple[str, ...]
    source_globs: tuple[str, ...]
    thin_references: tuple[str, ...]
    verification: tuple[str, ...]
    exclusive_literals: tuple[str, ...]
    exclusive_allowlist: tuple[str, ...]

@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    domain_id: str | None = None
    path: str | None = None

@dataclass(frozen=True)
class ValidationReport:
    schema_version: int
    valid: bool
    errors: tuple[ValidationIssue, ...]
    review_required: tuple[str, ...]
```

拒绝绝对路径、`..` 和仓库外解析结果；domain ID 使用 `^[a-z][a-z0-9-]*$`。TOML 顶层只允许
`schema_version`、`coverage`、`domains`，domain 只允许上述字段。

- [x] **Step 4: 创建两个试点 domain 的 manifest**

写入 `schema_version = 1`、`coverage = "pilot"`，登记：

- `runtime-observability` -> `docs/observability-harness.md`；
- `agent-eval` -> `evals/README.md`。

两个 domain 都把 `scripts/README.md` 列为薄入口；各自的 `source_globs` 覆盖当前实现和专项测试路径。
`agent-eval.exclusive_literals` 至少包含 webhook service name 与签名环境变量。

- [x] **Step 5: 运行测试并确认 GREEN**

Run: 使用 Step 2 同一命令。  
Expected: parser 相关测试全部通过。

---

### Task 2: 路由、排他事实与变更复核验证

**Files:**
- Modify: `scripts/check_documentation_authority.py`
- Modify: `tests/tdd/documentation-authority/test_documentation_authority.py`

**Interfaces:**
- Consumes: Task 1 的 `AuthorityManifest` 与 manifest domain。
- Produces: 错误码 `missing_authority`、`missing_thin_reference`、`authority_not_routed`、`exclusive_literal_leak`、`unmatched_source_glob`；dirty 或 `--git-range` 对应的 `review_required` domain ID。

- [x] **Step 1: 写路径、路由与排他事实 RED 测试**

覆盖以下结构化行为：

```python
assert _codes(validate_repository(repo)) == {"missing_authority"}
assert _codes(validate_repository(repo)) == {"authority_not_routed"}
assert _codes(validate_repository(repo)) == {"exclusive_literal_leak"}
```

另建测试证明：owner authority 内允许字面量、历史 `docs/superpowers/**` 不参与扫描、精确
`exclusive_allowlist` 可豁免一篇当前文档、目录级或 glob 豁免被拒绝。

- [x] **Step 2: 写 changed path 映射 RED 测试**

在临时 Git 仓库提交基线后修改 `evals/agent/example.py`，断言：

```python
report = authority.validate_repository(repo)
assert report.review_required == ("agent-eval",)
```

再用两次提交调用 `validate_repository(repo, git_range="HEAD~1..HEAD")`，证明显式 range 不混入 dirty
path，多个 domain 按 ID 排序且不重复。

- [x] **Step 3: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/documentation-authority/test_documentation_authority.py \
  -k 'route or literal or review or path'
```

Expected: validation is not implemented, assertions fail.

- [x] **Step 4: 实现确定性验证**

- 使用 `git ls-files --cached --others --exclude-standard` 建立仓库文件集合；
- 使用 `fnmatch.fnmatchcase` 匹配 `source_globs`；
- 默认从 `git status --porcelain=v1 -z` 读取 dirty/untracked path，`--git-range` 使用
  `git diff --name-only --find-renames <range> --`；
- 当前文档扫描范围为 `AGENTS.md`、`README.md`、manifest 中 authority/thin references、
  `tests/README.md` 和 `.codex/skills/**/SKILL.md`；排除历史目录；
- `coverage=pilot` 只检查 manifest authority 被 `AGENTS.md` 提及；`coverage=complete` 再反向检查
  AGENTS 中的当前 authority；
- 所有 issue 和 domain 输出稳定排序。

- [x] **Step 5: 实现 CLI JSON 与退出码**

支持：

```text
python scripts/check_documentation_authority.py --repo-root .
python scripts/check_documentation_authority.py --repo-root . --git-range BASE..HEAD
```

stdout 只写 JSON；预期校验错误写入 JSON 并退出 2，Git/IO 等无法完成检查的基础设施错误写 stderr 并退出
2；合法且仅有 `review_required` 时退出 0。

- [x] **Step 6: 运行定向测试并确认 GREEN**

Run: 使用 Step 3 同一命令。  
Expected: 全部通过。

---

### Task 3: Agent 路由、契约卡片与 workflow 集成

**Files:**
- Modify: `AGENTS.md`
- Modify: `docs/observability-harness.md`
- Modify: `evals/README.md`
- Modify: `scripts/README.md`
- Modify: `.codex/skills/assistant-agent-documentation-sync/SKILL.md`
- Modify: `.codex/skills/langfuse-eval-engineering/SKILL.md`
- Modify: `tests/tdd/documentation-authority/test_documentation_authority.py`

**Interfaces:**
- Consumes: `docs/authority.toml` 与 validator CLI。
- Produces: Agent 开始任务时的 manifest 路由、文档/路由变更完成门禁、两篇 authority 的固定契约卡片。

- [x] **Step 1: 写真实仓库 manifest RED 测试**

测试当前仓库运行 `validate_repository(REPO_ROOT)` 后：

```python
assert report.valid is True
assert {domain.id for domain in manifest.domains} == {
    "agent-eval",
    "runtime-observability",
}
```

并断言 webhook 排他字面量只在 `evals/README.md` 的当前文档范围出现。

- [x] **Step 2: 运行测试确认现有路由/卡片尚未满足约束**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/documentation-authority/test_documentation_authority.py \
  -k repository_manifest
```

Expected: manifest route or current-document ownership assertion fails.

- [x] **Step 3: 更新 `AGENTS.md`**

在任务路由前加入短规则：先读 `docs/authority.toml` 中匹配 domain，再读 authority；manifest 只服务工程
文档选择，不进入产品意图路由。完成规则增加：修改 current docs、AGENTS 路由、manifest 或其 validator
时必须运行 `scripts/check_documentation_authority.py --repo-root .`。

- [x] **Step 4: 为两个 authority 增加契约卡片**

在两篇文档开头、正文前加入相同字段顺序：`定位`、`Owns`、`Does not own`、`源码与 schema 入口`、
`验证入口`、`相邻 authority`。只引用现有正文，不复制 webhook、Score 或 runtime audit 细节。

- [x] **Step 5: 保持薄入口与 skill 只做路由**

- `scripts/README.md` 新增 validator 一行索引；
- documentation-sync skill 在 collector 前运行 validator，并分别说明结构验证与证据库存职责；
- Langfuse eval skill 删除可由 authority 查得的具体 webhook envelope/部署叙述，只保留执行顺序、门禁和
  指向 `evals/README.md` 的路由。

- [x] **Step 6: 运行真实仓库测试与 CLI**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/documentation-authority

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

Expected: pytest 通过；CLI JSON `valid=true`，退出 0，并列出当前 dirty path 命中的两个试点 domain。

---

### Task 4: 文档同步复核与交付验证

**Files:**
- Modify: `docs/superpowers/specs/2026-08-10-agent-first-documentation-architecture-design.md`
- Modify: `docs/superpowers/plans/2026-08-10-agent-first-documentation-authority.md`

**Interfaces:**
- Consumes: 前三项的 manifest、validator、路由和文档。
- Produces: 自洽的历史设计/计划状态与完整验证证据。

- [x] **Step 1: 更新设计和计划状态**

设计文档状态改为“已实施（阶段一）”；勾选本计划实际完成项。若实现与设计发生偏差，只记录真实最终
接口和原因，不保留互相矛盾的旧描述。

- [x] **Step 2: 运行完整临时测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/documentation-authority
```

Expected: 全部通过。根据 `tests/README.md` 不运行裸 pytest，因为产品 core invariant 未改变。

- [x] **Step 3: 运行 authority 与证据检查**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  .codex/skills/assistant-agent-documentation-sync/scripts/collect_documentation_evidence.py \
  --repo-root .
```

Expected: validator `valid=true`；collector 退出 0，当前 Markdown link 没有 `missing` 或
`missing_anchor`。

- [x] **Step 4: 运行 skill 与静态检查**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  /home/lenovo1/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/assistant-agent-documentation-sync

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  /home/lenovo1/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/langfuse-eval-engineering

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  scripts/check_documentation_authority.py

git diff --check
```

Expected: 所有命令退出 0。

- [x] **Step 5: 复核工作区和交付边界**

运行 `git status --short` 与 `git diff --name-status`，确认没有真实 `.env`、Provider 响应、Langfuse 数据或
其他生成物；报告阶段一完成内容、`review_required`、临时测试可删除性以及未迁移领域。当前工作区包含
此前同一对话的未提交 eval/runtime-audit 修改，不自动提交或拆分这些重叠文件。
