# 原生高自由度 Planner 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 planning 模式复用 `AssistantFastAgent` 的完整原生 Tool/Skill 循环先探索再生成结构化 DAG，并由确定性 scheduler、workers 与无 Tool finalizer 完成回答。

**Architecture:** Planner 不再直接调用 `model.with_structured_output`，而是以 `agent_phase="planner"` 调用同一个 `AssistantFastAgent`；phase middleware 只改变系统角色和结构化终态，不缩减首轮业务 Tool。真实 ToolMessage 转换为有界 `PlannerEvidence`，admission 只验证结构与授权，显式 scheduler 使用 LangGraph `Send` 按 wave 调度，revision/checkpoint/interrupt/resume 继续由原生 StateGraph 与 Agent Server 管理。

**Tech Stack:** Python 3.11、Pydantic v2、LangChain `create_agent`/middleware/ToolMessage、LangGraph `StateGraph`/`Send`/checkpoint、pytest、ruff。

**Spec:** `docs/superpowers/specs/2026-08-20-native-high-agency-planner-design.md`

## Global Constraints

- 只复用 `AssistantRootGraph`、`AssistantFastAgent`、LangGraph `StateGraph`、`Send`、middleware 和 Agent Server 原生生命周期；不新增 Runtime、scheduler service、队列或 checkpoint adapter。
- Planner 与 fast agent 的首轮业务 Tool projection 相同；不使用关键词、正则或手写意图规则预选 Tool、Skill 或 workflow。
- Planner 的 system prompt 规定“探索后规划”，终态为 `NativePlanProposal`；Planner/worker 可调用 Tool，finalizer 不可调用 Tool。
- Skill-governed Tool 只能由本轮真实成功的 `load_skill` grant 激活；worker 只继承 admission 确认的 Skill snapshot、直接依赖和显式 evidence refs。
- planning 非 read Tool 保留原生 HITL；fast 不触发 HITL；read Tool 保留官方 retry middleware。
- pytest 全部使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Provider。
- 临时 RED/GREEN 测试放入 `tests/tdd/native-high-agency-planner/`；只更新已被本次稳定行为改变的 `LOOP-001`、`CTX-001` core 测试。
- 修改 authority 后运行 `scripts/check_documentation_authority.py --repo-root .`。
- 源码修改后等待现有 8089 dev server hot reload，不启动第二个 dev server。
- 工作区已有用户改动；每次只暂存本任务文件，不回滚或提交无关改动。

## 文件职责

- `native_agent/models.py`：Planner evidence、deliverable 和 DAG schema。
- `native_agent/state.py`：Planner、admission、scheduler 与 worker 的原生 state channel。
- `native_agent/fast_agent.py`：fast/planner/worker 共享的唯一 `create_agent` 基座和可信 Skill prompt。
- `native_agent/planning_phase.py`：phase prompt、structured response、worker allowlist、finalizer Tool 清空。
- `native_agent/planning_graph.py`：Planner 调用、evidence、admission、scheduler、worker、revision、finalizer。
- `agent_server/services.py`：一次装配并共享 Tool inventory 与 Skill catalog。
- `tests/tdd/native-high-agency-planner/`：临时功能测试。
- `tests/core/integration/`：`LOOP-001`、`CTX-001` 的稳定契约更新。
- `docs/runtime-event-stream-architecture.md`、`docs/tool-calling-architecture.md`：当前 authority 同步。

---

### Task 1: 扩展严格计划与状态契约

**Files:**
- Modify: `src/assistant_agent/native_agent/models.py:1-100`
- Modify: `src/assistant_agent/native_agent/state.py:20-105`
- Create: `tests/tdd/native-high-agency-planner/test_plan_models.py`

**Interfaces:**
- Consumes: `EvidenceLink`、`WorkerResult`、`FastAgentState`。
- Produces: `PlannerEvidence`、`PlanDeliverable`、扩展的 `NativePlanNode`/`NativePlanProposal`、planning/worker state fields。

- [ ] **Step 1: 写失败测试**

```python
def test_plan_allows_zero_nodes_when_evidence_produces_deliverable() -> None:
    evidence = PlannerEvidence(
        evidence_id="tool-call-1",
        tool_name="weather_probe",
        status="succeeded",
        content="sunny",
    )
    proposal = NativePlanProposal(
        schema_version="native_plan_v1",
        nodes=(),
        deliverables=(PlanDeliverable(
            deliverable_id="answer",
            description="回答天气",
            evidence_refs=(evidence.evidence_id,),
        ),),
    )
    assert proposal.nodes == ()


def test_deliverable_requires_a_producer_or_evidence() -> None:
    with pytest.raises(ValidationError):
        PlanDeliverable(deliverable_id="answer", description="形成回答")
```

- [ ] **Step 2: 运行测试确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-high-agency-planner/test_plan_models.py
```

Expected: 新类型不存在，且 `nodes` 仍要求至少一个节点。

- [ ] **Step 3: 实现 schema 和 state**

```python
class PlannerEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    evidence_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,159}$")
    tool_name: str = Field(min_length=1, max_length=160)
    status: Literal["succeeded", "failed"]
    content: str = Field(min_length=1, max_length=20_000)
    structured_content: JsonValue | None = None
    artifact_ref: str | None = Field(default=None, max_length=2_000)


class PlanDeliverable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    deliverable_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,119}$")
    description: str = Field(min_length=1, max_length=2_000)
    producer_node_ids: tuple[str, ...] = Field(default=(), max_length=32)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def _has_producer(self) -> "PlanDeliverable":
        if not self.producer_node_ids and not self.evidence_refs:
            raise ValueError("deliverable requires a node producer or evidence")
        return self
```

给 `NativePlanNode` 增加唯一的 `evidence_refs`；给 `NativePlanProposal` 增加至少一个 `deliverables`，并允许 `nodes=()`。给 `PlanningState` 增加 `plan_candidate`、`planner_active_skill_ids`、`planner_skill_reference_grants`、`admission_error`、`revision_count`；`planner_evidence` 使用按 `evidence_id` 去重且保留先后顺序的 `Annotated[list[PlannerEvidence], _merge_planner_evidence]` reducer，确保 admission revision 不丢失真实证据。给 `WorkerState` 增加 `planner_evidence`。

- [ ] **Step 4: 再次运行 Task 1 测试，预期 PASS**

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/native_agent/models.py src/assistant_agent/native_agent/state.py \
  tests/tdd/native-high-agency-planner/test_plan_models.py
git commit -m "feat: extend native planning contracts"
```

---

### Task 2: 保持 Planner Tool 自由度并继承 Skill 指导

**Files:**
- Modify: `src/assistant_agent/native_agent/fast_agent.py:63-180,250-340`
- Modify: `src/assistant_agent/native_agent/planning_phase.py:1-120`
- Create: `tests/tdd/native-high-agency-planner/test_planning_phase.py`

**Interfaces:**
- Consumes: `FastAgentState.active_skill_ids`、`SkillCatalog.descriptors`、`planner_response_format()`。
- Produces: `render_assistant_system_prompt(..., active_skill_ids=...)`；Planner 保留上游可见 Tool；worker 空 allowlist fail closed。

- [ ] **Step 1: 写失败测试**

```python
def test_planner_preserves_all_upstream_visible_tools() -> None:
    projected = project_phase_request(
        phase="planner",
        tool_names=("load_skill", "weather_probe", "route_probe"),
    )
    assert tool_names(projected.tools) == {
        "load_skill", "weather_probe", "route_probe"
    }
    assert projected.response_format is not None


def test_fast_and_planner_first_business_tool_names_match() -> None:
    fast_names, planner_names = capture_first_model_tool_names(shared_agent)
    assert planner_names & inventory_names == fast_names & inventory_names


def test_worker_empty_allowlist_is_fail_closed() -> None:
    projected = project_phase_request(
        phase="worker", tool_names=("weather_probe",), worker_tool_allowlist=()
    )
    assert projected.tools == []


def test_active_skill_body_is_rendered_from_trusted_catalog() -> None:
    prompt = render_assistant_system_prompt(
        AssistantRunContext(),
        skill_descriptors=(descriptor,),
        active_skill_ids=(descriptor.name,),
    )
    assert "travel-sentinel-guidance" in prompt
```

同文件增加 finalizer `tools=[]` 且 `response_format is None` 的断言。测试 helper 构造标准 `ModelRequest` 并捕获 handler request，不访问 Provider。

- [ ] **Step 2: 运行测试确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-high-agency-planner/test_planning_phase.py
```

Expected: Planner 只剩控制 Tool；worker 空 allowlist 暴露全部 Tool；active Skill 正文未进入 prompt。

- [ ] **Step 3: 修改 phase projection**

```python
if phase == "planner":
    return request.override(
        response_format=planner_response_format(),
        system_message=_phase_system_message(request, planner_system_prompt()),
    )
if phase == "worker":
    allowed_names = _worker_tool_allowlist(request)
    return request.override(
        tools=[tool for tool in request.tools if _tool_name(tool) in allowed_names],
        response_format=None,
        model_settings=model_settings,
    )
```

Planner prompt 改为允许必要业务探索、复用共享证据、把独立深挖留给 DAG，并最终只提交 `NativePlanProposal`；删除“不执行业务工具”。

- [ ] **Step 4: 从可信 catalog 注入已激活 Skill 正文**

```python
return render_assistant_system_prompt(
    request.runtime.context,
    skill_descriptors=skill_index,
    active_skill_ids=tuple(request.state.get("active_skill_ids", ())),
)
```

`render_assistant_system_prompt` 只用 catalog 中与 `active_skill_ids` 相交的 descriptor.body 生成 `已加载专业流程` system 段；未知模型文本和未知 Skill ID 不参与渲染。

- [ ] **Step 5: 再次运行 Task 2 测试，预期 PASS**

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/native_agent/fast_agent.py \
  src/assistant_agent/native_agent/planning_phase.py \
  tests/tdd/native-high-agency-planner/test_planning_phase.py
git commit -m "feat: give planner shared native tool access"
```

---

### Task 3: 通过共享 Agent 执行 Planner 并捕获真实证据

**Files:**
- Modify: `src/assistant_agent/native_agent/planning_graph.py:1-140`
- Create: `tests/tdd/native-high-agency-planner/test_planner_execution.py`

**Interfaces:**
- Consumes: `AssistantFastAgent.ainvoke` 返回的 `structured_response`、`messages`、`active_skill_ids`、`skill_reference_grants`。
- Produces: `capture_planner_evidence(messages, *, inventory_names)`；Planner node 写 `plan_candidate` 和可信 Skill snapshot。

- [ ] **Step 1: 写失败集成测试**

```python
def test_planner_calls_default_tool_and_captures_real_evidence() -> None:
    result = asyncio.run(graph.ainvoke(planning_input, context=context))
    assert [(item.tool_name, item.content) for item in result["planner_evidence"]] == [
        ("weather_probe", "weather-sentinel")
    ]
    assert result["plan_candidate"].deliverables[0].evidence_refs == (
        result["planner_evidence"][0].evidence_id,
    )


def test_planner_loads_skill_then_calls_governed_tool() -> None:
    result = asyncio.run(skill_graph.ainvoke(planning_input, context=context))
    assert result["planner_active_skill_ids"] == ["travel-sentinel"]
    assert [item.tool_name for item in result["planner_evidence"]] == ["route_probe"]
```

Scripted model 第一轮调用 probe，下一轮提交 structured response；Skill 场景按 `load_skill -> governed Tool -> proposal` 执行。

- [ ] **Step 2: 运行测试确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-high-agency-planner/test_planner_execution.py
```

Expected: current planner 绕过 `AssistantFastAgent`，没有 evidence/grant state。

- [ ] **Step 3: 替换直接 structured model 调用**

```python
planner_result = await fast_agent.ainvoke(
    {
        "messages": list(state.get("messages", ())),
        "memory_context": tuple(state.get("memory_context", ())),
        "memory_status": state.get("memory_status", "empty"),
        "execution_mode": "planning",
        "trusted_runtime_facts": state.get("trusted_runtime_facts"),
        "agent_phase": "planner",
        "active_skill_ids": list(state.get("planner_active_skill_ids", ())),
        "skill_reference_grants": dict(state.get("planner_skill_reference_grants", {})),
    },
    context=runtime.context,
)
proposal = NativePlanProposal.model_validate(planner_result["structured_response"])
```

删除 `model.with_structured_output`。若存在 `admission_error`，只向 Planner 加一条有界修正说明，不暴露 Tool schema 或内部消息历史。

- [ ] **Step 4: 实现有界证据转换**

```python
def capture_planner_evidence(messages, *, inventory_names):
    evidence = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        if message.name not in inventory_names or message.name in _CONTROL_TOOL_NAMES:
            continue
        evidence.append(PlannerEvidence(
            evidence_id=message.tool_call_id,
            tool_name=message.name,
            status="failed" if message.status == "error" else "succeeded",
            content=_bounded_tool_content(message.content, max_chars=20_000),
            **_bounded_artifact(message.artifact),
        ))
    return tuple({item.evidence_id: item for item in evidence}.values())
```

只接受符合 `PlannerEvidence.evidence_id` pattern 且不超过 160 字符的原始 `tool_call_id`，不另造模型不可见的映射 ID；Planner prompt 明确要求 `evidence_refs` 使用已完成 ToolCall 的原始 ID。`_bounded_artifact` 用 Pydantic `to_jsonable_python` 规范化；序列化不超过 50,000 bytes 才保留 `structured_content`。超限时只递归接受长度不超过 2,000 的 `output_ref`/`artifact_ref`，不保存 Provider 原始响应。控制 Tool 不进入业务 evidence，其成功结果只通过既有 middleware 形成 Skill snapshot。

- [ ] **Step 5: 再次运行 Task 3 测试，预期 PASS**

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/native_agent/planning_graph.py \
  tests/tdd/native-high-agency-planner/test_planner_execution.py
git commit -m "feat: preserve planner tool evidence"
```

---

### Task 4: 增加确定性 admission policy 与 composition 共享事实

**Files:**
- Modify: `src/assistant_agent/native_agent/planning_graph.py:20-110`
- Modify: `src/assistant_agent/agent_server/services.py:20-105`
- Create: `tests/tdd/native-high-agency-planner/test_plan_admission.py`

**Interfaces:**
- Consumes: 静态 `Sequence[BaseTool]`、`SkillCatalog`、Planner active Skill 与 evidence。
- Produces: `PlanningAdmissionPolicy.from_inventory(...)`；扩展 `admit_native_plan(...)`。

- [ ] **Step 1: 写 admission 失败测试**

```python
@pytest.mark.parametrize("proposal", [
    proposal_with_unknown_tool(),
    proposal_with_fake_evidence(),
    proposal_with_ungranted_governed_tool(),
    proposal_with_cycle(),
    proposal_with_unknown_deliverable_producer(),
])
def test_admission_rejects_untrusted_plan_edges(proposal) -> None:
    with pytest.raises(NativePlanAdmissionError):
        admit_native_plan(
            proposal,
            policy=policy,
            evidence=evidence,
            active_skill_ids=(),
        )
```

再增加合法 default Tool 和已 grant governed Tool 的接受测试。

- [ ] **Step 2: 运行测试确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-high-agency-planner/test_plan_admission.py
```

Expected: current admission 不知道 Tool、evidence、grant 和 deliverable。

- [ ] **Step 3: 实现 admission policy**

```python
@dataclass(frozen=True)
class PlanningAdmissionPolicy:
    inventory_tool_names: frozenset[str]
    governed_tool_skills: Mapping[str, frozenset[str]]
    max_nodes: int = 128
    max_dependency_depth: int = 32

    @classmethod
    def from_inventory(cls, tools, skill_catalog):
        governed = {}
        for descriptor in skill_catalog.descriptors:
            for tool_name in descriptor.governed_tools:
                governed.setdefault(tool_name, set()).add(descriptor.name)
        return cls(
            inventory_tool_names=frozenset(tool.name for tool in tools),
            governed_tool_skills={
                name: frozenset(skill_ids) for name, skill_ids in governed.items()
            },
        )
```

`admit_native_plan` 验证 Tool 存在；governed Tool 的授权 Skill 必须同时出现在 Planner 实际 active Skill 和当前节点 `required_skill_ids` 中；evidence/deliverable 引用真实；DAG 无环且深度不超限。它不得读取用户文本或添加旅行领域规则。

- [ ] **Step 4: composition 一次加载并共享 catalog**

```python
skill_catalog = load_repo_skill_descriptors(default_repo_root())
fast_agent = build_fast_agent(..., skill_catalog=skill_catalog)
planning_graph = build_planning_graph(
    model, fast_agent, tools=tools, skill_catalog=skill_catalog
)
```

所有测试构建 planning graph 时也显式传入 probe Tool/catalog，不反射 compiled graph 私有结构。

- [ ] **Step 5: 再次运行 Task 4 测试，预期 PASS**

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/native_agent/planning_graph.py \
  src/assistant_agent/agent_server/services.py \
  tests/tdd/native-high-agency-planner/test_plan_admission.py
git commit -m "feat: admit planner dags against trusted inventory"
```

---

### Task 5: 实现显式 scheduler、窄 worker 输入和零节点 finalizer

**Files:**
- Modify: `src/assistant_agent/native_agent/planning_graph.py:90-360`
- Modify: `src/assistant_agent/native_agent/state.py:60-105`
- Create: `tests/tdd/native-high-agency-planner/test_scheduler.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py:160-205`

**Interfaces:**
- Consumes: admitted plan、PlannerEvidence、WorkerResult、Skill snapshot。
- Produces: `scheduler_node`、`route_scheduler`、隔离 WorkerState、finalizer payload。

- [ ] **Step 1: 写 wave、隔离和零节点失败测试**

```python
def test_scheduler_dispatches_only_ready_wave() -> None:
    first = route_scheduler(state_with_three_node_dag(results=[]))
    assert [send.arg["work_item_id"] for send in first] == ["weather", "food"]
    second = route_scheduler(state_with_three_node_dag(
        results=[result("weather"), result("food")]
    ))
    assert [send.arg["work_item_id"] for send in second] == ["itinerary"]


def test_worker_receives_only_scoped_inputs() -> None:
    send = route_scheduler(scoped_worker_state())[0]
    assert [item.work_item_id for item in send.arg["dependency_results"]] == ["weather"]
    assert [item.evidence_id for item in send.arg["planner_evidence"]] == ["route-call"]
    assert send.arg["active_skill_ids"] == ["travel-sentinel"]


def test_zero_node_plan_routes_directly_to_finalizer() -> None:
    assert route_scheduler(zero_node_state()) == "finalize"
```

- [ ] **Step 2: 运行测试确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-high-agency-planner/test_scheduler.py
```

Expected: 无 scheduler node；零节点报错；worker 没有 evidence/Skill snapshot。

- [ ] **Step 3: 改为显式原生拓扑**

```python
builder.add_edge(START, "planner")
builder.add_edge("planner", "admit_plan")
builder.add_conditional_edges("admit_plan", route_after_admission)
builder.add_conditional_edges("scheduler", route_scheduler)
builder.add_edge("worker", "join")
builder.add_edge("join", "scheduler")
builder.add_edge("finalize", END)
```

`route_scheduler` 按 plan 顺序稳定生成 `Send`，只装配直接依赖、节点 evidence refs、节点 allowlist，以及 required Skill 与 Planner snapshot 的交集；没有节点或全部完成时返回 `finalize`。

- [ ] **Step 4: 让 worker 继承受限状态**

worker 调用共享 Agent 时增加 `active_skill_ids`、`skill_reference_grants`。`_worker_prompt` 分别输出只读 `<dependency_results trust="untrusted">` 和 `<planner_evidence trust="tool-output">`，两者都不能覆盖系统、用户、身份或 Tool 授权。

- [ ] **Step 5: finalizer 复用共享 Agent 且清空 Tool**

finalizer JSON 包含原请求、deliverables、全部 PlannerEvidence 和按 plan 排序的 WorkerResult；以 `agent_phase="finalizer"` 调用同一 `AssistantFastAgent`，由 phase middleware 确定性 `tools=[]`，只把 terminal `AIMessage` 写回父 state。

- [ ] **Step 6: 更新 `LOOP-001` 并验证**

更新既有 core 测试：planning graph 显式包含 `planner/admit_plan/scheduler/worker/join/finalize`；Planner/worker/finalizer 复用 `AssistantFastAgent`；两个 root worker 同 wave 并行；零节点仍得到标准 AIMessage。

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-high-agency-planner/test_scheduler.py \
  tests/core/integration/test_runtime_lifecycle.py
```

Expected: all pass，独立 worker 最大并发仍为 2。

- [ ] **Step 7: 提交**

```bash
git add src/assistant_agent/native_agent/planning_graph.py \
  src/assistant_agent/native_agent/state.py \
  tests/tdd/native-high-agency-planner/test_scheduler.py \
  tests/core/integration/test_runtime_lifecycle.py
git commit -m "feat: schedule native planning dag waves"
```

---

### Task 6: 用原生 graph revision 修复无效计划并保留 HITL/checkpoint

**Files:**
- Modify: `src/assistant_agent/native_agent/planning_graph.py:60-230`
- Create: `tests/tdd/native-high-agency-planner/test_native_revision.py`
- Modify: `tests/core/integration/test_context_lifecycle.py:130-205`

**Interfaces:**
- Consumes: `admission_error`、`revision_count`、LangGraph conditional edge、Agent Server/checkpointer。
- Produces: `route_after_admission`；最多两次 proposal revision；Planner/worker planning HITL。

- [ ] **Step 1: 写 revision 和 checkpoint 恢复失败测试**

```python
def test_invalid_plan_reenters_planner_through_native_edge() -> None:
    result = asyncio.run(graph.ainvoke(planning_input, context=context))
    assert result["revision_count"] == 1
    assert result["admission_error"] is None
    assert result["plan"].nodes[0].node_id == "valid-worker"


def test_scheduler_recomputes_ready_nodes_after_checkpoint_resume() -> None:
    interrupted = asyncio.run(checkpointed_graph.ainvoke(planning_input, config=config))
    resumed = asyncio.run(checkpointed_graph.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}), config=config
    ))
    assert interrupted["__interrupt__"]
    assert [item.work_item_id for item in resumed["worker_results"]] == [
        "write-worker", "dependent-worker"
    ]
```

Scripted Planner 首次提交 unknown Tool，第二次提交合法计划；通过 stream updates 验证重新进入 `planner`，不读取 saver。

- [ ] **Step 2: 运行测试确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-high-agency-planner/test_native_revision.py
```

Expected: admission error 直接逃逸，没有 graph revision。

- [ ] **Step 3: 用 state 和条件边实现受限 revision**

```python
MAX_PLAN_REVISIONS = 2

def admit_plan_node(state):
    try:
        admitted = admit_native_plan(...)
    except NativePlanAdmissionError as exc:
        revision_count = int(state.get("revision_count", 0)) + 1
        if revision_count > MAX_PLAN_REVISIONS:
            raise
        return {"admission_error": str(exc), "revision_count": revision_count}
    return {"plan": admitted, "admission_error": None}

def route_after_admission(state):
    return "planner" if state.get("admission_error") else "scheduler"
```

revision 保留既有 evidence/Skill snapshot，只覆盖 `plan_candidate`；超限后抛出有界 admission error，由原生 run failure 表达。

- [ ] **Step 4: 扩展 `CTX-001` HITL 测试**

覆盖 Planner write Tool 在执行前 interrupt、approve 后继续形成 proposal；worker write Tool 保持 interrupt/resume；恢复后 scheduler 从 checkpointed plan/results 推导下一节点。另断言 fast 模式同一 write Tool 不触发 planning HITL。

- [ ] **Step 5: 运行定向验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-high-agency-planner/test_native_revision.py \
  tests/core/integration/test_context_lifecycle.py \
  tests/core/integration/test_memory_lifecycle.py
```

Expected: all pass；interrupt 使用原生 `Command(resume=...)`；父图 Memory snapshot 正常传入 Planner/worker。

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/native_agent/planning_graph.py \
  tests/tdd/native-high-agency-planner/test_native_revision.py \
  tests/core/integration/test_context_lifecycle.py
git commit -m "feat: revise invalid plans through langgraph state"
```

---

### Task 7: 同步 authority 并完成离线与 8089 验证

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md:35-100`
- Modify: `docs/tool-calling-architecture.md:20-80`
- Modify: `docs/context_engineering_status.md:20-45`
- Verify: Tasks 1-6 的全部文件

**Interfaces:**
- Consumes: 最终源码、测试和 authority contracts。
- Produces: 与源码一致的权威说明、校验结果和现有 8089 reload 证据。

- [ ] **Step 1: 更新 runtime authority**

写明 Planner 以 `agent_phase="planner"` 复用 `AssistantFastAgent`；PlannerEvidence/deliverables/Skill snapshot；`admit_plan -> scheduler -> Send(worker) -> join -> scheduler`；零节点 finalizer；admission revision 的原生 conditional edge/checkpoint；finalizer 清空 Tool 并返回标准 AIMessage。删除“当前不维护 deliverable/revision/evidence”的过期陈述。

- [ ] **Step 2: 更新 Tool authority**

写明 fast/planner 首轮 Tool projection 等价；Planner Tool 调用仍走 retry/HITL/ToolNode；成功 `load_skill` 让后续 Planner 和 admission 后 worker 获得 governed Tool 与可信 Skill 正文；worker 空 allowlist fail closed；finalizer 无 Tool。

同时更新 `docs/context_engineering_status.md`：Skill L0 index 仍用于发现；成功 `load_skill` 写入的 `active_skill_ids` 使后续 model call 从受信 catalog 将完整 Skill 正文追加到 system prompt，worker 只继承 admission 确认的 active Skill snapshot。

- [ ] **Step 3: 运行完整 feature 与受影响 core**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/native-high-agency-planner \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py \
  tests/core/integration/test_memory_lifecycle.py \
  tests/core/contract/test_tool_contract.py \
  tests/core/contract/test_extension_contract.py \
  tests/core/contract/test_observability_contract.py
```

Expected: all selected tests pass in mock/offline mode.

- [ ] **Step 4: 运行 lint、diff 和文档校验**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/native_agent/models.py src/assistant_agent/native_agent/state.py \
  src/assistant_agent/native_agent/fast_agent.py src/assistant_agent/native_agent/planning_phase.py \
  src/assistant_agent/native_agent/planning_graph.py src/assistant_agent/agent_server/services.py \
  tests/tdd/native-high-agency-planner tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
git diff --check
```

Expected: ruff clean；authority script 成功；无 whitespace error。

- [ ] **Step 5: 等待并验证现有 8089 hot reload**

```bash
curl --fail --silent --show-error http://127.0.0.1:8089/ok
```

确认现有服务后等待 reload，再用公开 SDK/HTTP 创建一个 mock planning sentinel run，断言 terminal 是标准 AIMessage，原生 updates/trace 出现 `planner`、`admit_plan`、`scheduler`、`finalize`。若 8089 未运行，只报告状态；不启动相同工作目录的第二实例。

- [ ] **Step 6: 记录 operator-gated 行为验收，不自动调用真实 Provider**

默认只记录下列待验收用例和观测字段，不执行 real run：

1. 开放请求“明天我想去安吉玩漂流”：检查 Planner 是否自适应考虑天气、漂流攻略、交通、住宿判断、餐饮和相关景点，且独立任务形成并行 wave；
2. 窄请求“规划从 A 到 B 的驾车路线”：检查计划保持窄范围并保留高德路线链接；
3. 两例都记录 Planner Tool 次数、worker Tool 次数、重复 Tool、scheduler wave、总延迟和最终覆盖度。

只有用户再次明确授权 real mode、Provider 配置完整并启用 operator 开关时，才通过现有 `assistant-native-v1` 执行这两例；最终报告需单列真实调用范围和结果。不得为这一步新增 mock fallback 或第二套行为 runner。

- [ ] **Step 7: 检查范围并提交 authority**

```bash
git status --short
git diff -- src/assistant_agent/native_agent src/assistant_agent/agent_server/services.py \
  tests/tdd/native-high-agency-planner tests/core/integration \
  docs/runtime-event-stream-architecture.md docs/tool-calling-architecture.md
git add docs/runtime-event-stream-architecture.md docs/tool-calling-architecture.md \
  docs/context_engineering_status.md
git commit -m "docs: describe native high-agency planning"
```

确认每个代码提交只含本任务文件。设计规格与本计划按仓库规则保持未跟踪，除非用户明确要求纳入版本控制。

最终汇报包含：完成内容；`Core invariant: LOOP-001 and CTX-001 updated`；实际测试命令和结果；真实 Provider 未调用；8089 reload 状态；临时测试目录可由用户手动删除；限制和后续建议。
