# AI Coding Stage 5E：原生行为评测基线实施计划

> **执行方式：** 按 task 顺序严格 RED/GREEN；每个 task 独立提交生产文件。设计、计划与临时 TDD 默认不提交。

**Goal:** 通过现有 `8089` Agent Server 对真实 `AssistantCodingGraph` 建立 operator-gated、deterministic-grader 的端到端行为评测基线。

**Spec:** `docs/superpowers/specs/2026-08-25-ai-coding-stage-5e-behavior-evaluation-design.md`

**约束:** mock/offline 开发；不调用真实 Provider；不修改生产 CodingGraph；不恢复旧 Runtime/Release Review；不新增永久 pytest。

## Task 1：评测 contract 与 dry-run

**生产文件：**

- `src/assistant_agent/evaluation/coding_behavior.py`
- `evals/system/ai_coding_behavior/cases.json`
- `tests/tdd/ai-coding-behavior-eval/test_contracts.py`（临时）

- [ ] RED：严格 case/schema、路径、catalog、集合关系、预算和 dry-run 安全断言。
- [ ] 实现 `CodingBehaviorCase`、suite/result/check/error contract 与受信 catalog ID。
- [ ] 实现纯函数 dry-run report；证明不读取 Provider secret、不连接 Server、不创建 repo。
- [ ] GREEN 并提交生产文件：`feat: define coding behavior evaluation contracts`。

## Task 2：临时 Git fixture 与 deterministic graders

**生产文件：**

- `evals/system/ai_coding_behavior/fixtures.py`
- `evals/system/ai_coding_behavior/graders.py`
- `tests/tdd/ai-coding-behavior-eval/test_fixtures_graders.py`（临时）

- [ ] RED：四类 fixture、base digest、held-out tests、actual path scope、forbidden path、merge binding、cleanup 边界。
- [ ] fixture 只能由静态 ID 构建，不接受宿主路径或任意 argv。
- [ ] grader command 只来自静态 catalog，stdout/stderr 只形成有界 digest/error 分类。
- [ ] GREEN 并提交生产文件：`feat: add deterministic coding behavior graders`。

## Task 3：原生 Agent Server coding eval driver

**生产文件：**

- `src/assistant_agent/evaluation/coding_agent_server.py`
- `tests/tdd/ai-coding-behavior-eval/test_agent_server_driver.py`（临时）

- [ ] RED：公开 SDK thread/run、coding input、interrupt/resume、terminal、deadline、未知 interrupt、重复/超预算、identity/repo binding。
- [ ] driver 只投影原生 transition evidence，不复制 CodingGraph 状态机。
- [ ] 自动审批仅允许 fixture patch/review/merge；dependency/credential/artifact/未知 interrupt fail closed。
- [ ] transport/cancel/permission 与模型质量失败分类分离。
- [ ] GREEN 并提交生产文件：`feat: drive coding behavior eval through agent server`。

## Task 4：正式 runner、artifact 与 CLI

**生产文件：**

- `evals/system/ai_coding_behavior/runner.py`
- `evals/system/ai_coding_behavior/__init__.py`
- `scripts/run_system_ai_coding_behavior_eval.py`
- `tests/tdd/ai-coding-behavior-eval/test_runner.py`（临时）

- [ ] RED：默认 dry-run、real mode、双授权开关、固定 8089、suite ID、repo config binding、脱敏 artifact、cleanup pending。
- [ ] runner 逐 case 创建临时 repo，经 reload nonce 与 Server composition attestation 两阶段绑定 opaque repo ID，驱动 native run、执行 graders、聚合 fail-closed result。
- [ ] evidence 不含源码/patch/messages/prompt/secret/raw Provider response。
- [ ] CLI `--help` 与 `--dry-run` 离线可运行；真实模式本阶段不执行。
- [ ] GREEN 并提交生产文件：`feat: add coding behavior system eval runner`。

## Task 5：安全加固、authority 与最终验证

**生产文件：**

- `evals/README.md`
- `scripts/README.md`
- `docs/authority.toml`（仅 verification/source_globs 确需变化时）
- `docs/runtime-event-stream-architecture.md`（只记录 evaluation does-not-own 边界，若 owner review 需要）
- `tests/tdd/ai-coding-behavior-eval/test_hardening.py`（临时）

- [ ] RED：symlink/path traversal、manifest extra fields、case duplication、oversize evidence、malicious interrupt payload、server URL 非 loopback、artifact secret-like keys。
- [ ] 修复所有 scoped Critical/Important finding，不扩大到生产 CodingGraph。
- [ ] 运行完整 Stage 5E TDD、Stage 5D covering、core、authority validator、compileall、CLI dry-run、`8089 /ok`。
- [ ] 独立复审 spec compliance 与代码质量。
- [ ] 提交 authority/入口：`docs: document coding behavior evaluation baseline`。

## 最终验收命令

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-behavior-eval

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-review-repair

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_ai_coding_behavior_eval.py --dry-run --suite baseline-v1

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/evaluation evals/system/ai_coding_behavior \
  scripts/run_system_ai_coding_behavior_eval.py

git diff --check
curl -fsS http://127.0.0.1:8089/ok
```

## 完成定义

- 全部 task 生产提交存在且 worktree tracked clean；
- 临时 TDD、spec、plan 与 SDD 报告保持未提交；
- 默认路径没有 Provider/网络/Git mutation；
- 正式真实入口具备 operator 门禁，但本阶段未执行真实 Provider；
- Stage 5F 只基于未来真实 evidence 立项，不在本阶段预实现恢复逻辑。
