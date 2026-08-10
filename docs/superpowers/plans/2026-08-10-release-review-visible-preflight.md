# Release Review 可见 Preflight 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Langfuse Remote Experiment 在启动异步评测前同步发现目录不兼容，并让默认 Scenario 只依赖真实生产工具目录中的能力。

**Architecture:** 新增只读 CLI preflight，复用 Scenario loader、Provider 配置和生产 `AgentGraphRuntime` 目录装配；webhook 在返回 `202` 前调用它，失败映射为非 2xx。Decision fixture 仍只替换工具执行结果，不注册模拟工具；现有天气/本地联网案例改用生产目录已有工具表达相同风险。

**Tech Stack:** Python、FastAPI、Pydantic、pytest、Langfuse Remote Experiment。

## Global Constraints

- pytest 必须 `mock/local/offline`，不得调用真实 Provider 或外部工具。
- 真实运行仍要求 real Provider、签名、Staging readiness 和显式副作用许可。
- 不修改 `tests/core`；临时测试继续放在 `tests/tdd/release-review-native-experiment/`。
- 不改动或提交工作区其他任务文件，本计划本身不提交。

---

### Task 1: 同步 webhook preflight

**Files:**
- Modify: `src/assistant_agent/evaluation/release_review.py`
- Modify: `src/assistant_agent/api/routes_eval_experiments.py`
- Modify: `evals/release_review/cli.py`
- Test: `tests/tdd/release-review-native-experiment/test_release_review_webhook.py`
- Test: `tests/tdd/release-review-native-experiment/test_release_service.py`

**Interfaces:**
- Consumes: 已验证的 `ReleaseReviewPayload` 与固定 CLI argv。
- Produces: `--preflight` CLI 模式，以及启动子进程前的同步成功/失败结果。

- [ ] **Step 1: Write the failing test**

新增断言：launcher 先执行固定 preflight argv；preflight 非零时不调用 `Popen`，并产生可映射为非 2xx 的结构化异常；CLI preflight 只检查所选场景的 required tools。

- [ ] **Step 2: Run test to verify it fails**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment/test_release_review_webhook.py tests/tdd/release-review-native-experiment/test_release_service.py`

Expected: FAIL，因为 launcher 尚未执行同步 preflight，CLI 也没有 `--preflight`。

- [ ] **Step 3: Write minimal implementation**

为 CLI 增加与 `--run` 共用选择和目录检查的 `--preflight`；launcher 使用注入式 `run_factory` 同步执行固定命令，失败时抛出 `ReleaseReviewPreflightFailed`；FastAPI 在线程池执行 launcher，并把异常映射为 `503`。

- [ ] **Step 4: Run test to verify it passes**

Run 同 Step 2，Expected: PASS。

### Task 2: 默认 Scenario 对齐生产目录

**Files:**
- Modify: `evals/release_review/scenarios/correct_tool_among_candidates.yaml`
- Modify: `evals/release_review/scenarios/no_unobserved_result_claim.yaml`
- Modify: `evals/release_review/scenarios/tool_failure_no_repeat.yaml`
- Modify: `evals/release_review/scenarios/wait_for_tool_result.yaml`
- Test: `tests/tdd/release-review-native-experiment/test_release_scenarios.py`

**Interfaces:**
- Consumes: 生产目录已有的 `calendar_search` 与 `mcp.amap_maps.maps_geo` ToolSpec。
- Produces: 不再要求 `weather` 或本地 `web_search` 的固定 Git Scenario。

- [ ] **Step 1: Write the failing test**

断言全部默认 Decision scenario 的 required tools 不包含 `weather` / `web_search`，并保留工具选择、等待结果、失败不重试和无证据不声称四项风险。

- [ ] **Step 2: Run test to verify it fails**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment/test_release_scenarios.py`

Expected: FAIL，列出当前四个过期依赖。

- [ ] **Step 3: Write minimal implementation**

将三个天气案例改为 `calendar_search`，将无观测结果案例改为 `mcp.amap_maps.maps_geo` 失败；同步 request、arguments、fixture 与 sequence，保持 capability/risk/state assertion 不变。

- [ ] **Step 4: Run test to verify it passes**

Run 同 Step 2，Expected: PASS。

### Task 3: 文档与整体验证

**Files:**
- Modify: `evals/README.md`
- Modify: `.codex/skills/langfuse-eval-engineering/SKILL.md`

**Interfaces:**
- Consumes: 新的同步 preflight 契约。
- Produces: operator 可见的 UI 失败语义与完整运行说明。

- [ ] **Step 1: Update documentation**

说明 webhook 返回 `202` 前会做生产目录 preflight，失败直接显示为 Remote Experiment 错误；异步接受后才会创建 Dataset Run。

- [ ] **Step 2: Run complete targeted verification**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment`

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_release_review.py --inspect`

Expected: 全部通过，11 个 scenario / 13 个 Dataset item，默认 required tools 无 `weather` / `web_search`。

- [ ] **Step 3: Commit**

只暂存上述源码、Scenario、测试和权威文档；不暂存本计划与工作区其他改动。
