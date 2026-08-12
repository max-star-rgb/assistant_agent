# Workflow Plan v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 durable Workflow 的模型规划协议升级为通用、可验证、可版本化兼容的 `workflow_plan_v2`，并移除 Deep Research 对节点名称和 `kind` 的语义猜测。

**Architecture:** Planner 只生成一个静态 DAG 版本；v2 用显式节点、类型化步骤验收契约、交付物责任绑定和约束责任绑定描述计划。Controller 在 admission 时确定性验证 DAG、交付物覆盖和 verifier 可达性，再转换为现有持久执行模型；旧 v1 proposal 和既有持久计划继续可读、可执行，但新的 planner prompt 只请求 v2。

**Tech Stack:** Python 3.11、Pydantic v2、现有 Durable Workflow controller、pytest 临时 TDD。

## Global Constraints

- `workflow_plan_v2` 只包含通用 Workflow 核心，不包含 research 专用节点类型或 source-count 专用 schema。
- 单个 admitted plan version 必须是静态无环图；拓扑变化只能通过新的 plan version 表达。
- 普通 assistant request 继续走现有 ReAct loop；只有可信入口启动的 Durable Workflow 才调用 planner。
- Planner proposal 是不可信输入；所有引用、唯一性、覆盖关系与可达性均由 controller 确定性验证。
- v1 proposal 与已有持久化 `WorkflowPlanVersion` 保持读取和执行兼容。
- 默认 mock/offline，不调用真实 Provider，不新增依赖。
- 保留当前工作区已有未提交修改，不覆盖无关 diff。

---

### Task 1: 定义 v2 通用计划与类型化验收模型

**Files:**
- Modify: `src/assistant_agent/workflows/models.py`
- Create: `tests/tdd/workflow-plan-v2/test_workflow_plan_v2.py`

**Interfaces:**
- Produces: `WorkflowAcceptanceCriterion`、`WorkflowArtifactContract`、`WorkflowStepAcceptanceContract`、`WorkflowPlanNodeV2`、`WorkflowDeliverableBindingProposal`、`WorkflowConstraintProposalV2`、`WorkflowPlanV2Proposal`、`WorkflowPlannerProposal`。
- Compatibility: 保留 `WorkflowPlanProposal` 与 `WorkflowSeedWorkItem` 作为 v1 wire model。

- [ ] **Step 1: 写 v2 schema RED 测试**

```python
def test_v2_plan_requires_typed_node_acceptance_and_deliverable_ownership():
    proposal = WorkflowPlanV2Proposal.model_validate({
        "schema_version": "workflow_plan_v2",
        "nodes": [{
            "node_id": "collect",
            "display_title": "收集证据",
            "objective": "收集并记录一手证据。",
            "depends_on": [],
            "acceptance_contract": {
                "output": {
                    "artifact_type": "evidence_set",
                    "description": "可供下游使用的证据集合",
                },
                "criteria": [{
                    "criterion_id": "sources-recorded",
                    "statement": "每条证据包含来源和检索时间。",
                }],
            },
        }],
        "deliverable_bindings": [{
            "deliverable": "research_report",
            "producer_node_id": "collect",
        }],
        "constraint_bindings": [],
    })
    assert proposal.nodes[0].acceptance_contract.output.artifact_type == "evidence_set"
```

- [ ] **Step 2: 运行测试并确认因 v2 类型尚不存在而失败**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/workflow-plan-v2/test_workflow_plan_v2.py`

Expected: collection/import FAIL，缺少 `WorkflowPlanV2Proposal`。

- [ ] **Step 3: 实现最小 Pydantic v2 wire models**

约束：ID 使用稳定 pattern；节点列表、criteria、deliverable bindings 均非空；`extra="forbid"`；同一对象内 ID 不重复。`WorkflowPlannerProposal` 是 v1/v2 union，不删除 v1 类型。

- [ ] **Step 4: 添加 malformed schema 用例并完成 GREEN**

覆盖空 criteria、重复 node ID、重复 criterion ID、未知 schema version 和额外字段。

- [ ] **Step 5: 运行 Task 1 定向测试**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/workflow-plan-v2/test_workflow_plan_v2.py`

Expected: PASS。

### Task 2: 建立 v2 normalization、交付物绑定和静态 DAG admission

**Files:**
- Modify: `src/assistant_agent/workflows/models.py`
- Modify: `src/assistant_agent/workflows/definitions.py`
- Modify: `src/assistant_agent/workflows/transitions.py`
- Modify: `src/assistant_agent/workflows/runtime.py`
- Test: `tests/tdd/workflow-plan-v2/test_workflow_plan_v2.py`

**Interfaces:**
- Consumes: `WorkflowPlannerProposal`。
- Produces: `materialize_planner_proposal(proposal, deliverables) -> tuple[list[WorkflowWorkItem], list[WorkflowConstraintProposal], list[WorkflowDeliverableBinding]]`。
- Persists: `WorkflowPlanVersion.deliverable_bindings`，旧记录缺省为空。

- [ ] **Step 1: 写 admission RED 测试**

分别证明：未知依赖、cycle、重复交付物、漏交付物、未知 producer、非 terminal producer、未知 constraint owner、verifier 不在所有 owner 下游均被拒绝。

- [ ] **Step 2: 运行 RED 并确认当前代码接受至少一个非法 v2 计划**

Run: Task 1 同一命令。

- [ ] **Step 3: 实现 v2 normalization 与 plan-version 持久字段**

v2 节点统一 materialize 为 `kind="agent"`；`depends_on` 同时表示控制依赖和上游 artifact 输入。v1 继续沿用原字段，并把 terminal work item 兼容映射为交付物 producer。

- [ ] **Step 4: 扩展 `validate_plan_dag`**

在现有拓扑和 constraint reachability 校验之外，验证交付物唯一覆盖、producer 引用以及 producer terminal 性。拓扑不可原地改变的规则继续由 plan version/CAS 边界保证。

- [ ] **Step 5: 运行 Task 2 定向测试并确认 GREEN**

Run: Task 1 同一命令。

Expected: PASS。

### Task 3: 升级 planner prompt 与严格解析协议

**Files:**
- Modify: `src/assistant_agent/workflows/agent_runtime.py`
- Modify: `src/assistant_agent/workflows/definitions.py`
- Modify: `tests/tdd/deep-research-plan-execute/test_plan_execute_runtime.py`
- Test: `tests/tdd/workflow-plan-v2/test_workflow_plan_v2.py`

**Interfaces:**
- Produces: `render_work_item_prompt()` 的 v2 planner 外壳；`parse_workflow_plan_response()` 同时解析 v2 和 v1。
- Changes: bootstrap planner acceptance marker 从 `workflow_plan_v1` 改为 `workflow_plan_v2`。

- [ ] **Step 1: 写 parser RED 测试**

传入严格的 v2 envelope，断言得到 `WorkflowPlanV2Proposal`；传入 legacy v1 envelope，断言仍成功；传入 prose、额外顶层字段或非法 v2 schema，断言 `workflow_plan_invalid`。

- [ ] **Step 2: 运行 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/workflow-plan-v2 tests/tdd/deep-research-plan-execute/test_plan_execute_runtime.py`

- [ ] **Step 3: 实现清晰分区的 planner prompt**

Prompt 顺序固定为：角色、禁止事项、输入 JSON、规划规则、验收规则、输出 schema、最终输出要求。规则明确：不执行任务、不创建 `plan` 节点、只生成静态 DAG、每节点只承担自己的 acceptance、每个 deliverable 恰有一个 terminal producer、required constraint 必须有下游 verifier。

- [ ] **Step 4: 实现 v2-first/v1-compatible parser**

新 prompt 只请求 v2；parser 使用带 `schema_version` 的 v2 model 或无版本 v1 model，不接受模糊混合结构。

- [ ] **Step 5: 运行 Task 3 测试并确认 GREEN**

Run: Step 2 命令。

### Task 4: 通用化 definition，移除研究节点名称推断

**Files:**
- Modify: `src/assistant_agent/workflows/definitions.py`
- Modify: `src/assistant_agent/workflows/builtin.py`
- Modify: `src/assistant_agent/workflows/research/definition.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `tests/tdd/durable-workflow-e2e/test_workflow_definitions_execution.py`
- Test: `tests/tdd/workflow-plan-v2/test_workflow_plan_v2.py`

**Interfaces:**
- Produces: 所有 definition 共用的 v2 materialization 路径。
- Removes: `_SOURCE_COLLECTION_KINDS`、`_EVIDENCE_AGGREGATION_KINDS`、`_FINAL_WORK_KINDS` 和 `_source_constraint_bindings()`。

- [ ] **Step 1: 写通用化 RED 测试**

使用完全不含 research/collect/verify/synthesize 等名称的节点，证明同一 v2 schema 可以 materialize Deep Research 和 Long Horizon；交付物与约束责任只由显式 binding 决定。

- [ ] **Step 2: 运行 RED 并确认旧 research kind 推断无法满足测试**

- [ ] **Step 3: 将 Deep Research definition 收窄为 submission validation + 通用 materialization**

`source_target` 不再由 definition 猜测节点并绑定；可信 Deep Research ingress 将“至少 15 个来源”作为普通 workflow constraint 提交，planner 必须显式分配 owner/verifier。

- [ ] **Step 4: 统一 Long Horizon materialization**

两个 definition 调用相同 helper；业务 definition 不解释节点名字。

- [ ] **Step 5: 运行 Task 4 定向测试并确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/workflow-plan-v2 tests/tdd/durable-workflow-e2e/test_workflow_definitions_execution.py`

### Task 5: 用交付物绑定决定最终结果并保持 v1 行为

**Files:**
- Modify: `src/assistant_agent/workflows/runtime.py`
- Modify: `src/assistant_agent/workflows/models.py`
- Test: `tests/tdd/workflow-plan-v2/test_workflow_plan_v2.py`
- Modify: `tests/tdd/durable-workflow-e2e/test_workflow_result_protocol.py`

**Interfaces:**
- Consumes: `WorkflowPlanVersion.deliverable_bindings`。
- Changes: Workflow 完成时按 submission deliverable 顺序收集 producer artifacts；v1 无 binding 时保留 terminal item fallback。

- [ ] **Step 1: 写多 terminal 节点 RED 测试**

构造一个非交付 terminal 节点排在交付节点之后，断言最终 `result_artifact_refs` 仍来自显式 producer，而不是“最后完成节点”。

- [ ] **Step 2: 运行 RED**

- [ ] **Step 3: 实现显式 result artifact 汇聚与 v1 fallback**

- [ ] **Step 4: 运行 Task 5 定向测试并确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/workflow-plan-v2 tests/tdd/durable-workflow-e2e/test_workflow_result_protocol.py`

### Task 6: 同步 authority 并完成验证

**Files:**
- Modify: `docs/tool-calling-architecture.md`
- Review: `tests/tdd/workflow-plan-v2/`

**Interfaces:**
- Documents: v2 wire contract、静态 plan version、显式 deliverable/constraint binding、v1 compatibility 与 definition 通用边界。

- [ ] **Step 1: 更新当前 authority**

只记录当前架构事实，不把本计划或外部产品调研复制进 authority。

- [ ] **Step 2: 运行完整 feature 定向测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/workflow-plan-v2 \
  tests/tdd/deep-research-plan-execute \
  tests/tdd/durable-workflow-e2e
```

- [ ] **Step 3: 运行文档 authority 校验**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

- [ ] **Step 4: 复核 diff 和兼容证据**

确认无真实 Provider 调用、无新依赖、无 research kind 推断、v2 planner 严格输出、v1 fixtures 仍通过，并保留任务开始前已有的未提交改动。

