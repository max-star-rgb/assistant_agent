# AI Coding 阶段 2 受控验证闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在阶段 1 的隔离 worktree、patch 校验和 HITL 基础上，增加由服务端固定配置驱动的 test/lint/format/build 验证闭环。

**Architecture:** `CodingValidationService` 在一次性 scratch 副本中按仓库配置的有序 command IDs 执行固定 argv；模型不能提供 argv、cwd、环境或 shell。Graph 只在已批准 patch 应用后调用该 service；test/lint/build 的任何写入随 scratch 丢弃，format 产生的增量 patch 经现有 `CodingWorkspaceService.validate_patch()` 校验并再次进入 digest-bound HITL，批准后才应用到 thread-scoped worktree。

**Tech Stack:** Python 3.12、Pydantic、LangGraph StateGraph/interrupt、Git CLI 固定 argv、`subprocess.Popen`、POSIX `resource` limits、pytest。

**Spec:** `docs/superpowers/specs/2026-08-20-ai-coding-agent-design.md`

## Global Constraints

- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，测试不得访问真实 Provider 或网络。
- 命令只能由受信配置中的 command ID 映射到固定 argv；不接受 `bash -c`、管道、重定向、命令替换、任意 cwd 或模型自定义环境。
- command cwd 固定为一次性 scratch 仓库根；不得在 source repo、Agent Server 当前目录或受管 coding worktree 中直接启动验证进程。
- 子进程环境只保留受信最小变量；不得继承 API key、token、proxy 或真实 `.env`。
- 每条命令必须限制 wall timeout、CPU、地址空间、进程数、单文件输出、捕获输出和 scratch 总字节。
- 阶段 2 仍不提供 delete、rename、Git commit、merge、push、PR、部署、依赖安装、任意 shell 或网络隔离保证。
- formatter 的增量 diff 必须通过阶段 1 的路径、文本、大小、base digest 和 patch digest 校验，并独立 HITL；最多接受一轮 formatter patch，第二次仍产生 diff 时 fail closed。
- 新测试放入 `tests/tdd/ai-coding-validation/`，不加入默认 pytest，不提交该临时目录。

---

### Task 1: 定义 command allowlist 与验证证据契约

**Files:**
- Modify: `src/assistant_agent/coding/config.py`
- Modify: `src/assistant_agent/coding/models.py`
- Modify: `.env.example`
- Create: `tests/tdd/ai-coding-validation/test_command_config.py`

**Interfaces:**
- Produces: `CodingCommandConfig(command_id, kind, argv, timeout_seconds, cpu_seconds, memory_bytes, max_processes, max_output_bytes, max_disk_bytes)`
- Changes: `CodingRepositoryConfig.commands: dict[str, CodingCommandConfig]`
- Changes: `CodingRepositoryConfig.verification_sequence: tuple[str, ...]`
- Produces: `CodingCommandEvidence`、`CodingVerificationResult`

- [ ] **Step 1: 写配置 RED 测试**

覆盖：合法的 test/lint/format/build 固定 argv；command key 必须等于 `command_id`；sequence 只能引用同仓库 command；拒绝空 argv、NUL/newline、shell executable、`-c` shell 模式和重复 ID。确认模型输入 schema 与 `AssistantRootInput` 不出现 argv/cwd/env 字段。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-validation/test_command_config.py
```

Expected: FAIL，配置模型尚无 command contract。

- [ ] **Step 3: 实现严格 Pydantic 配置**

`CodingCommandConfig` 使用 frozen/strict/extra-forbid；`kind` 只允许四类；`argv` 在 parse 前把 JSON list 归一化为 tuple。拒绝 `sh`、`bash`、`dash`、`zsh`、`fish`、`cmd`、`powershell` 等 shell basename，拒绝空参数、控制字符和 `-c` shell 入口。`CodingRepositoryConfig` 的 after validator 校验 key、sequence 引用和 command ID 唯一性；未配置 sequence 时保持阶段 1 行为。

- [ ] **Step 4: 增加稳定结果模型**

`CodingCommandEvidence` 仅保存 command ID/kind/status/exit code/duration/output digest/有界 stdout/stderr/truncated；`CodingVerificationResult` 保存 overall status、evidence tuple 和可选 formatter `CodingPatchValidation`。不保存 argv、cwd、宿主路径或完整未截断输出。

- [ ] **Step 5: 运行 GREEN 并提交生产契约**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-validation/test_command_config.py
git add .env.example src/assistant_agent/coding/config.py src/assistant_agent/coding/models.py
git commit -m "feat: define controlled coding validation commands"
```

---

### Task 2: 实现 scratch validation service 与资源边界

**Files:**
- Create: `src/assistant_agent/coding/validation.py`
- Create: `tests/tdd/ai-coding-validation/conftest.py`
- Create: `tests/tdd/ai-coding-validation/test_validation_runner.py`

**Interfaces:**
- Produces: `CodingValidationService(workspace_service)`
- Produces: `run(workspace, repository, *, format_round) -> CodingVerificationResult`
- Consumes: `CodingWorkspaceService.validate_patch()` 生成 formatter 增量 proposal

- [ ] **Step 1: 写 runner RED 测试**

使用临时 Git repo 和当前 Python 解释器作为固定 executable，覆盖：固定 cwd 为 scratch；原 worktree 不被命令写入；环境中不存在测试注入的 secret/proxy；成功、非零退出、timeout、输出截断、scratch 超限均返回稳定状态；command evidence 不泄露 scratch 绝对路径或 argv。

- [ ] **Step 2: 写 formatter RED 测试**

固定 formatter 脚本只修改 scratch 中的一个允许文本文件。断言 runner 返回由 `validate_patch()` 产生的增量 `CodingPatchValidation`，实际 workspace 未变化；非法删除、二进制写入、protected path 或第二轮仍产生 formatter diff 时返回稳定失败码。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-validation/test_validation_runner.py
```

Expected: FAIL，validation service 不存在。

- [ ] **Step 4: 实现一次性 scratch**

在 configured workspace root 的独立 `validation/` 临时目录复制工作区内容，排除 `.git`、管理 metadata、socket/device/FIFO 和逃逸 symlink；在 scratch 内用固定 Git argv 建立 index baseline。每条命令使用新的 scratch，完成后无条件清理。

- [ ] **Step 5: 实现受限进程执行**

用 `Popen(argv, cwd=scratch, shell=False, start_new_session=True)`；env 仅包含固定 `PATH`、locale、`HOME`/`TMPDIR` 指向 scratch。`preexec_fn` 设置 `RLIMIT_CPU`、`RLIMIT_AS`、`RLIMIT_NPROC`、`RLIMIT_FSIZE`；stdout/stderr 写入 scratch 临时文件。wall timeout 时 kill 整个 process group；读取有界输出并计算完整文件 digest；运行后扫描 scratch 总字节，超限 fail closed。

- [ ] **Step 6: 实现 formatter diff 捕获**

命令成功后对新文件执行固定 `git add -N -- <paths>`，再以固定 Git argv生成 UTF-8 unified diff。非 format 命令忽略并丢弃 scratch 写入；format 无 diff 继续 sequence，有 diff则调用 `workspace_service.validate_patch(workspace, patch, bounded_summary)` 并立即返回 `format_approval_required`；第二个 format round 再产生 diff 返回 `format_not_idempotent`。

- [ ] **Step 7: 运行 GREEN 并提交 service**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-validation/test_validation_runner.py
git add src/assistant_agent/coding/validation.py
git commit -m "feat: run coding validation in bounded scratch copies"
```

---

### Task 3: 接入 CodingGraph 与 formatter 二次 HITL

**Files:**
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/coding_graph.py`
- Create: `tests/tdd/ai-coding-validation/test_validation_graph.py`

**Interfaces:**
- Changes: `build_coding_graph(..., validation_service: CodingValidationService)`
- Changes: `CodingState.verification_evidence`、`format_round`、`approval_origin`
- Changes: `CodingTerminalResult.verification_status`、`verification_evidence`

- [ ] **Step 1: 写 Graph RED 测试**

用 scripted inspect agent、临时 checkpointer 与 fake validation service 断言：初始 patch 未审批前不运行命令；批准并 apply 后才验证；全部通过时 terminal 保留结构化 evidence；test/lint/build 失败时不再次调用模型且返回稳定 error code。

- [ ] **Step 2: 写 formatter HITL RED 测试**

断言 formatter validation 触发第二个 `coding_patch_apply` interrupt，payload 增加 `origin=formatter` 且绑定新 digest；错误 digest/reject 不应用 formatter patch；正确 digest 只应用对应增量 patch后重跑验证；第二次 formatter diff fail closed。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-validation/test_validation_graph.py
```

Expected: FAIL，Graph 在 apply 后直接 summarize。

- [ ] **Step 4: 扩展顺序 Graph**

目标结构：

```text
... -> approval -> apply_patch -> run_validation
run_validation -> summarize | approval(formatter)
approval(formatter) -> apply_patch -> run_validation
```

初始 patch 与 formatter patch 共用 validator、digest-bound interrupt 和确定性 apply；`approval_origin` 只由 Graph 写入。`apply_patch` 不再提前构造 terminal result；验证成功后才构造带 evidence 的 `CodingTerminalResult(status="applied", verification_status="passed")`。

- [ ] **Step 5: 运行 GREEN 并提交 Graph**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-validation/test_validation_graph.py
git add src/assistant_agent/native_agent/state.py src/assistant_agent/native_agent/coding_graph.py
git commit -m "feat: verify approved coding patches"
```

---

### Task 4: 生产 composition 与核心结构保护

**Files:**
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Create: `tests/tdd/ai-coding-validation/test_validation_composition.py`

**Interfaces:**
- Changes: process owner owns one `CodingValidationService`
- Changes: `LOOP-001` records deterministic post-apply validation and formatter return edge
- Preserves: fast/planning Tool inventory has no coding command Tool

- [ ] **Step 1: 写 composition RED 测试**

断言 owner 只构造一份 validation service 并注入 coding graph；coding graph 出现 `run_validation` 节点；普通 Tool inventory 不出现 validation/shell Tool；无 sequence 的阶段 1 配置不启动进程并正常结束。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-validation/test_validation_composition.py
```

Expected: FAIL，composition 尚未注入 service。

- [ ] **Step 3: 装配并关闭 validation service**

`AgentServerExecutionOwner.compose()` 只解析一次 `CodingConfig`，用同一配置构造 workspace/validation service 并注入 graph。service 不持有 run-local subprocess；`aclose()` 仅清理其临时资源，不创建新 Runtime。

- [ ] **Step 4: 更新最小 core 结构断言**

`LOOP-001` 的 coding 子图稳定节点集合加入 `run_validation`。不为具体 command、输出文本、配置常量或 formatter 实现增加永久测试；现有 `CTX-001` 的 digest-bound patch HITL 已覆盖初始与 formatter patch，不新增测试数量。

- [ ] **Step 5: 运行 GREEN 与受影响 core 并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-validation/test_validation_composition.py tests/core/integration/test_runtime_lifecycle.py
git add src/assistant_agent/agent_server/services.py tests/core/INVARIANTS.md tests/core/integration/test_runtime_lifecycle.py
git commit -m "feat: compose governed coding validation"
```

---

### Task 5: 同步 authority 并完成阶段验收

**Files:**
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/authority.toml`

**Interfaces:**
- Documents: command allowlist、scratch 生命周期、资源边界、Graph evidence、formatter HITL、阶段 2 非目标
- Changes manifest: `src/assistant_agent/coding/validation.py` owned by agent-server/tool-calling as contract permits

- [ ] **Step 1: 更新 owner authority 与 manifest**

Agent Server authority 记录 process-owned validation service 和 scratch 生命周期；runtime authority 记录 post-apply validation/formatter approval edge；tool authority 明确 validation command 不是模型 Tool、固定 argv 和无 shell。manifest 将新模块路由到对应 owner，并增加阶段 2 定向 TDD 命令。

- [ ] **Step 2: 运行阶段 2 全部定向测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-validation tests/tdd/ai-coding-patch tests/tdd/ai-coding-workspace tests/core/integration/test_runtime_lifecycle.py
```

- [ ] **Step 3: 运行 authority validator**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
```

- [ ] **Step 4: 通过现有 8089 验证 hot reload**

只作为客户端检查 `/ok` 和 mock coding run；不启动、停止或重启用户管理的服务。验证 disabled/no-sequence 保持兼容，并用显式临时配置验证结构化 command evidence；不调用真实 Provider。

- [ ] **Step 5: 提交 authority**

```bash
git add docs/agent-server-architecture.md docs/runtime-event-stream-architecture.md docs/tool-calling-architecture.md docs/authority.toml
git commit -m "docs: document coding validation boundaries"
```

## 完成汇报格式

```text
完成：AI Coding 阶段 2 受控验证闭环。
Core invariant: LOOP-001 extended with deterministic post-apply validation; CTX-001 behavior remains digest-bound for every patch, including formatter output.
Tests: added tests/tdd/ai-coding-validation for temporary RED/GREEN; user may delete this directory manually.
Validation: <实际命令与结果>。
Provider: 未调用真实 Provider；全部验证使用 mock/offline。
Limitations: 无任意 shell、无依赖安装、无强网络隔离、无 commit/merge/push；强 sandbox 属阶段 4。
```
