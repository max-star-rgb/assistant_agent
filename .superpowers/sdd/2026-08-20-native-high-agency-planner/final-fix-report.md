# Native high-agency planner final fix report

## 状态

完成。两个 Important、三个 deferred minor closure，以及追加的 production Skill catalog 单次加载审查项均已关闭。实现提交为 `eaab0060`（`fix: bound native planning failure contexts`）。本轮仅运行 mock/local/offline 测试；未调用真实 Provider，未操作 8089，也未创建第二套 runtime。

## 实现摘要

### Important 1：worker 终态失败边界

- `worker` 节点使用 LangGraph 原生 `RetryPolicy(max_attempts=3)` 与 node `error_handler`。
- 仅明确的 timeout、connection、HTTP 408/409/425/429/5xx operational failure 在重试耗尽后转为固定、无异常正文的 failed `WorkerResult`。
- error handler 通过原生 `Command(update=..., goto="join")` 写入 root worker 失败；scheduler 下一 super-step 生成依赖 blocked result，再进入 finalizer。
- classifier 检查完整、cycle-safe 的 cause/context 链；任何层级的 permission、cancel、admission 或 programmer-contract error 都优先保持原生异常传播。LangGraph 自身对 `GraphBubbleUp` / interrupt 的原生旁路保持不变。
- compiled production-boundary 回归验证：root 恰好尝试 3 次、child 0 次、root update 在 blocked child update 前可见、无重复 result、finalizer 收到按 plan 排序的失败/限制，最终仅一个标准 `AIMessage`，且 Provider secret sentinel 不进入 state/prompt。

### Important 2：端到端 context 与 artifact 预算

- worker 最终 escaped/rendered prompt 上限精确为 `MAX_WORKER_CONTEXT_CHARS = 48_000`，覆盖 direct dependency results 与所引用 PlannerEvidence。
- finalizer 最新单条 `HumanMessage` 上限精确为 `MAX_FINALIZER_CONTEXT_CHARS = 96_000`，覆盖 request、deliverables、全部 PlannerEvidence 与按 plan 排序的 WorkerResults。
- 两个 projector 均先保留 ID、status、sources、artifact refs，再对所有 content slot 使用相同字符 cap 做确定性公平分配；输出只生成完整 JSON，并设置结构化 truncation marker。
- `_bounded_artifact` 不再先调用 `to_jsonable_python` 完整物化 artifact。现在按 depth 8、最多访问 512 个 mapping/sequence item 增量遍历，active-container cycle detection，遍历时过滤 raw/unsafe key，未知对象不 stringify；最终 structured JSON 不超过 50,000 bytes。
- 在边界内遇到的首个可信 `output_ref` / `artifact_ref` 单独保留。byte/item/depth/cycle/unknown/non-finite/string 超限均返回 JSON-safe marker；投影接近 50,000 bytes 时会从 bounded 局部尾部让出空间，保证 marker 不丢失。

### Deferred minor closure

1. `plan_candidate is None` 已进入 admission 的统一 try/revision path：前两次产生 `missing_candidate` revision update，第三次按相同预算抛出有 code 的 admission error。
2. 补齐 admission 表驱动覆盖：multi-governing Skill any-of、exact/over/diamond depth、duplicate deliverable producers、duplicate deliverable evidence refs、duplicate PlannerEvidence IDs；未改变既有正确语义。
3. compiled failed-root 测试同时覆盖 super-step 可见性、dependent 不执行与 blocked result 不重复。

### 追加阻塞项：Skill catalog 单次加载

- 复核发现当前 `AgentServerExecutionOwner.compose()` 已在 inventory 前加载一次 repo `SkillCatalog` 并把同一实例传给 inventory、fast agent 与 planning graph；残余风险是 `create_native_tool_inventory(..., skill_catalog=None)` 仍允许 `SkillLoadingPlugin.build_tools()` 静默走自己的 loader。
- 将 production inventory、built-in 构造和 plugin 列表装配的 `skill_catalog` 参数改为必填，消除该 fallback 在 production inventory 的可达性。
- 回归直接 monkeypatch `assistant_agent.tools.plugins.builtin.skill_loading.plugin.load_repo_skill_descriptors`，证明漏传 catalog 会在进入实际 plugin loader 前因签名失败；composition 回归同时记录 service loader、plugin loader、inventory/plugin/fast/planning 收到的对象身份，断言 service 恰好加载一次、plugin loader 零次且四处共享同一实例。

## TDD RED / GREEN 证据

### RED

1. Worker production boundary：

   ```text
   pytest -q test_scheduler.py::test_compiled_worker_exhaustion_blocks_dependents_before_finalizing \
     test_scheduler.py::test_worker_failure_boundary_does_not_convert_non_operational_errors
   -> F..；root TimeoutError 直接终止 graph，无法传播 blocked dependency。
   ```

2. Context/artifact budgets：

   ```text
   pytest -q tests/tdd/native-high-agency-planner/test_context_budgets.py
   -> 3 failed；worker prompt 1,431,396 chars，finalizer message 661,780 chars，artifact traversal 超过 512 items。
   ```

3. Missing candidate revision：focused test RED，`missing_candidate` 在 admission try 外直接抛出。
4. Catalog blocker：

   ```text
   pytest -q test_plan_admission.py::test_native_tool_inventory_requires_catalog_before_skill_plugin_build
   -> 1 failed；未抛 TypeError，实际进入 plugin loader fallback。
   ```

5. 自审安全边界：wrapped permission/type 两例 RED（被外层 TimeoutError 错误转换）；8 层 cause 链 permission RED；CancelledError 带 timeout cause RED。
6. Artifact marker：接近 50,000-byte 的投影 RED（守住 byte cap，但 truncation marker 被丢弃）。

### GREEN

- Worker 初始 focused GREEN：`3 passed in 2.91s`。
- Context/artifact 初始 focused GREEN：`3 passed in 2.87s`。
- Missing candidate focused GREEN：`1 passed`。
- Catalog + production identity + core extension focused GREEN：`4 passed in 4.74s`。
- Wrapped permission/type + compiled operational exhaustion GREEN：`3 passed in 2.96s`。
- Huge/cyclic artifact + marker reservation GREEN：`2 passed in 2.66s`。
- Deep permission chain focused GREEN：`1 passed in 2.98s`。
- Cancellation focused GREEN：`1 passed in 2.89s`（首个修复尝试暴露缺少 `asyncio` import，补齐后才记 GREEN）。
- Admission table characterization：`8 passed, 14 deselected`。

## 最终 fresh 门禁

```text
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/tdd/native-high-agency-planner
-> 63 passed in 5.80s

MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/core
-> 52 passed in 6.88s

MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_tool_contract.py \
  tests/core/contract/test_extension_contract.py \
  tests/core/integration/test_runtime_lifecycle.py
-> 14 passed in 6.19s

python -m ruff check <all changed Python files>
-> All checks passed!

python -m ruff format --check <all changed Python files>
-> 9 files already formatted

python scripts/check_documentation_authority.py --repo-root .
-> valid=true, errors=[]; reviewed owners: agent-server, runtime-event-stream, test-policy, tool-calling

git diff --check
-> passed
```

## 文件

- `src/assistant_agent/native_agent/planning_graph.py`
- `src/assistant_agent/native_agent/tools.py`
- `tests/tdd/native-high-agency-planner/test_context_budgets.py`
- `tests/tdd/native-high-agency-planner/test_scheduler.py`
- `tests/tdd/native-high-agency-planner/test_native_revision.py`
- `tests/tdd/native-high-agency-planner/test_plan_admission.py`
- `tests/tdd/native-high-agency-planner/test_planner_execution.py`
- `tests/core/contract/test_extension_contract.py`
- `tests/core/integration/test_runtime_lifecycle.py`
- `docs/runtime-event-stream-architecture.md`
- `docs/authority.toml`

## 自审结论

- 对照 brief 逐项复核了 retry/error-handler 原生性、异常分类优先级、failed-root super-step、最终 rendered 字符计数、artifact 增量遍历、catalog 实例身份、authority ownership 与测试归属。
- 未发现请求 scope 内仍未关闭的问题；Tool/Skill governance、HITL、领域语义、real Provider 和 Agent Server 端口均未改变。

## Concerns / 基线门禁

- 仓库级 `python -m ruff check .` 仍因未修改的 `scripts/run_system_multimodal_embedding_eval.py:18` 既有 E402 失败；本任务 changed-file ruff check 全绿。
- 仓库级 `python -m ruff format --check .` 报告 180 个与本任务无关的既有文件会被 reformat；为避免扩大 scope 未批量改写。全部 9 个本任务 Python 文件 format check 全绿。
- `tests/tdd/native-high-agency-planner` 受仓库 `.gitignore` 管理，本轮新增测试已用 force-add 纳入实现提交；其余 ignored cache/data/review artifacts 均未提交。
