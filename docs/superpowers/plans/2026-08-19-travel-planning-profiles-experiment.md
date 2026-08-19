# 旅行 Planning 与限定联网实验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不承诺生产合入的隔离实验分支中，验证 `create_agent` Planner/Worker 复用、逐 Worker `Send` 能力投影、百炼预枚举搜索 profile 与来源保留四个高风险假设，并产出明确的继续/修订/终止结论。

**Architecture:** 使用官方 `custom workflow + create_agent + structured output + Send + middleware` 原语建立最小闭环。实验先以 deterministic fake model 和 fake transport 离线证明框架行为，再以最多两次、operator 显式授权的 Qwen/DashScope 真实请求核对严格站点限制；任何关键门槛失败都停止后续生产迁移，不通过访问 LangGraph 私有 API 或复制 Agent loop 绕过。

**Tech Stack:** Python 3.12、LangChain `>=1.3.15,<2`、LangGraph `>=1.2.4,<2`、Pydantic v2、pytest、DashScope Generation API、项目 `hello_agent` conda 环境。

**Spec:** `docs/superpowers/specs/2026-08-19-travel-planning-search-profiles-design.md`

## Global Constraints

- 执行前使用 `superpowers:using-git-worktrees` 创建隔离 worktree；实验提交不得直接合入当前开发分支。
- 默认和 pytest 始终设置 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得读取或调用真实 Provider。
- 真实 Provider 只允许在 Task 7 中，由 operator 同时显式设置 real mode 与 `--allow-real-provider` 后执行；计划调用上限为 2 次。
- 不修改高德 MCP schema、现有 `lodging_search` 行为或 `travel-tool-orchestration` 的生产 manifest。
- 不新增 `travel_guide_search`、`travel_app_handoff`、`select_search_profile` 或自定义 Agent Runtime。
- LLM 只能选择预枚举 profile 和受信 catalog 中已登记的 Tool 名；域名与 Provider 参数只能来自本地 policy registry。
- `worker_tool_allowlist=[]` 表示显式禁止全部本地 Tool；字段缺失才表示不应用 planning Worker 限制。
- 实验不修改 `tests/core` 或 `tests/core/INVARIANTS.md`。若实验通过并进入生产实施，后续计划必须将 `LOOP-001` 更新为 Planner/Worker/Finalizer 的 phase 复用契约。
- 所有临时 RED/GREEN 测试放在 `tests/tdd/travel-planning-experiment/`，用户可在实验结束后手动整目录删除；不得自动晋升 core。
- 实验 artifact 只写入未跟踪的 `.data/evals/system/travel_planning_profiles/`，不得保存 prompt、回答正文、API key、Provider 原始响应或用户数据。

---

## 实验问题与决策门

| Gate | 问题 | 通过条件 | 失败动作 |
| --- | --- | --- | --- |
| G1 | 同一个 compiled `AssistantFastAgent` 能否在 Planner phase 先调用 `load_skill`，随后通过动态 `response_format` 返回 `NativePlanProposal`？ | 标准 `ToolMessage` 配对完整；`structured_response` 通过 Pydantic 校验；不调用业务 Tool | 停止复用同一实例；修订规格为同一 factory/middleware 栈构造 role-specific Planner Agent 与 Worker Agent |
| G2 | 同一个 Worker compiled graph 能否由并行 `Send` 获得互不污染的 Skill、Tool allowlist 和 search profile？ | 三个 Worker 并行；观察到的 Tool 集合与输入逐项相等；空 allowlist 为零 Tool；无跨分支 state 泄漏 | 停止实现；不得改用全局变量、ContextVar 或手工复制 Agent loop |
| G3 | DashScope adapter 能否对六个预枚举 profile fail closed 地生成确定 payload？ | `none` 显式关闭；严格 profile 使用 `turbo + assigned_site_list`；未知/不支持 profile 在网络前失败 | 停止 real eval，修订 profile 或 Provider 协议设计 |
| G4 | Worker 搜索来源能否穿过 planning reducer 到 Finalizer，而不由 LLM 重建 URL？ | terminal metadata 来源机械归一化进入 `WorkerResult.sources`；并行汇总后仍与 work item 一一对应 | 停止 App 链接设计，先修订 WorkerResult/Finalizer 契约 |
| G5 | 百炼真实请求是否遵守 12306 与小红书严格站点范围？ | 两次请求均返回正文；若有来源，全部域名属于对应 allowlist；无来源明确记为 inconclusive，不算通过 | 不推广生产；保留离线结果并修订站点/profile 策略 |

## 文件结构

实验 worktree 中创建或修改以下文件：

```text
src/assistant_agent/native_agent/
├── models.py                         # 实验性 plan/worker 结构字段
├── state.py                          # phase、Worker allowlist/profile state
├── fast_agent.py                     # 插入 phase middleware
├── planning_experiment.py            # 最小实验 Graph；不替换生产 planning_graph
├── planning_phase.py                 # phase prompt/tool/response-format 投影
└── search_profiles.py                # 封闭 profile policy 与 capability 校验

src/assistant_agent/providers/
└── dashscope_langchain.py            # 实验 profile payload 支持；生产未接线

tests/tdd/travel-planning-experiment/
├── probes.py                         # scripted model、probe tools、fake transport
├── test_planner_create_agent.py      # G1
├── test_worker_send_projection.py    # G2
├── test_search_profile_payloads.py   # G3
├── test_worker_sources.py            # G4
└── test_mock_experiment_graph.py     # G1-G4 离线闭环

evals/system/travel_planning_profiles/
├── __init__.py
├── README.md                         # operator 门禁、调用数、artifact 与删除条件
└── runner.py                         # dry-run 与最多两次真实 Provider 调用
```

`planning_experiment.py` 是刻意隔离的 spike，不修改 `planning_graph.py`。实验通过后不得直接把 spike 重命名为生产实现；应另写生产实施计划，根据结论最小迁移现有 Graph。

---

### Task 1: 建立封闭数据契约与搜索 Policy

**Files:**
- Modify: `src/assistant_agent/native_agent/models.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Create: `src/assistant_agent/native_agent/search_profiles.py`
- Create: `tests/tdd/travel-planning-experiment/test_search_profile_payloads.py`

**Interfaces:**
- Consumes: 现有 `NativePlanNode`、`NativePlanProposal`、`FastAgentState`。
- Produces: `AgentPhase`、`ProviderSearchProfile`、`SearchProfilePolicy`、`resolve_search_profile()`、带 `allowed_tool_names` 的 plan node，以及 Worker 所需 state 字段。

- [ ] **Step 1: 写封闭枚举与 policy 的 RED 测试**

```python
import pytest
from pydantic import ValidationError

from assistant_agent.native_agent.models import NativePlanNode
from assistant_agent.native_agent.search_profiles import (
    SearchProfileCapabilityError,
    resolve_search_profile,
)


def test_profile_registry_is_closed_and_none_disables_search() -> None:
    policy = resolve_search_profile("none", protocol="dashscope", model_name="qwen-plus")
    assert policy.enable_search is False
    assert policy.assigned_site_list == ()

    with pytest.raises(SearchProfileCapabilityError):
        resolve_search_profile("model-supplied-domain", protocol="dashscope", model_name="qwen-plus")


def test_plan_node_rejects_unknown_profile_and_keeps_empty_tool_scope() -> None:
    node = NativePlanNode(
        node_id="rail",
        objective="rail-sentinel",
        required_skill_ids=("travel-tool-orchestration",),
        allowed_tool_names=(),
        search_profile="rail_official",
    )
    assert node.allowed_tool_names == ()

    with pytest.raises(ValidationError):
        NativePlanNode(
            node_id="unsafe",
            objective="unsafe-sentinel",
            search_profile="https://example.com",
        )
```

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel-planning-experiment/test_search_profile_payloads.py
```

Expected: collection fails because `search_profiles` and new model fields do not exist.

- [ ] **Step 3: 实现最小数据契约**

在 `models.py` 定义：

```python
ProviderSearchProfile = Literal[
    "none",
    "rail_official",
    "flight_official",
    "guide_official",
    "guide_xiaohongshu",
    "travel_general",
]

class NativePlanNode(BaseModel):
    # 保留现有 model_config、node_id、objective、depends_on 与 validator
    required_skill_ids: tuple[str, ...] = Field(default=(), max_length=16)
    allowed_tool_names: tuple[str, ...] = Field(default=(), max_length=64)
    search_profile: ProviderSearchProfile = "none"
```

在 `state.py` 定义：

```python
AgentPhase = Literal["fast", "planner", "worker", "finalizer"]

class FastAgentState(AgentState):
    # 保留现有字段
    agent_phase: NotRequired[AgentPhase]
    worker_tool_allowlist: NotRequired[tuple[str, ...]]
    provider_search_profile: NotRequired[ProviderSearchProfile]
```

在 `search_profiles.py` 定义不可由模型修改的 registry：

```python
@dataclass(frozen=True)
class SearchProfilePolicy:
    profile: ProviderSearchProfile
    enable_search: bool
    search_strategy: Literal["turbo"] | None
    forced_search: bool
    assigned_site_list: tuple[str, ...]
    prompt_intervene: str | None

_POLICIES = {
    "none": SearchProfilePolicy("none", False, None, False, (), None),
    "rail_official": SearchProfilePolicy(
        "rail_official", True, "turbo", True, ("12306.cn",),
        "仅检索中国铁路12306官方公开信息",
    ),
    "guide_xiaohongshu": SearchProfilePolicy(
        "guide_xiaohongshu", True, "turbo", True,
        ("xiaohongshu.com",), "仅检索公开可索引的小红书旅行内容",
    ),
    "flight_official": SearchProfilePolicy(
        "flight_official", True, "turbo", True, ("caac.gov.cn",),
        "仅检索民航主管部门、机场或航空公司的官方公开信息",
    ),
    "guide_official": SearchProfilePolicy(
        "guide_official", True, "turbo", True, (),
        "仅检索文旅主管部门或景区运营主体的官方公开信息",
    ),
    "travel_general": SearchProfilePolicy(
        "travel_general", True, "turbo", True, (),
        "仅检索与用户目的地和日期直接相关的公开旅行信息",
    ),
}
```

上述域名仅用于验证 policy 与 payload 机制，不代表最终生产航司/机场覆盖表；实验不得把它宣称为完整国内航班来源。

`resolve_search_profile()` 必须验证：profile 存在、protocol 为 `dashscope`、严格站点数量不超过 25；任何失败均在发起网络请求前抛出 `SearchProfileCapabilityError`。

- [ ] **Step 4: 添加六个 profile 的参数化断言并运行 GREEN**

测试必须逐项断言：

```python
@pytest.mark.parametrize(
    ("profile", "enabled"),
    [
        ("none", False),
        ("rail_official", True),
        ("flight_official", True),
        ("guide_official", True),
        ("guide_xiaohongshu", True),
        ("travel_general", True),
    ],
)
def test_all_profiles_are_predeclared(profile: str, enabled: bool) -> None:
    assert resolve_search_profile(
        profile, protocol="dashscope", model_name="qwen-plus"
    ).enable_search is enabled
```

Run: 使用 Step 2 相同命令。  
Expected: PASS。

- [ ] **Step 5: 提交实验契约**

```bash
git add src/assistant_agent/native_agent/models.py \
  src/assistant_agent/native_agent/state.py \
  src/assistant_agent/native_agent/search_profiles.py \
  tests/tdd/travel-planning-experiment/test_search_profile_payloads.py
git commit -m "experiment: define travel planning capability contracts"
```

---

### Task 2: 验证 Planner `create_agent` 的 Skill→结构化计划循环（G1）

**Files:**
- Create: `src/assistant_agent/native_agent/planning_phase.py`
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Create: `tests/tdd/travel-planning-experiment/probes.py`
- Create: `tests/tdd/travel-planning-experiment/test_planner_create_agent.py`

**Interfaces:**
- Consumes: `AgentPhase`、`NativePlanProposal`、现有 `ProgressiveToolExposureMiddleware`、真实 `create_load_skill_tool()`。
- Produces: `PlanningPhaseMiddleware`, `planner_response_format()`；`build_fast_agent()` 构造的同一个 graph 可通过 `agent_phase="planner"` 运行。

- [ ] **Step 1: 写 Planner loop 的 RED 测试**

测试使用 scripted `BaseChatModel`：第一次只调用 `load_skill`；观察到匹配的 `ToolMessage` 后，调用 LangChain `ToolStrategy(NativePlanProposal)` 提供的结构化提交工具。关键断言：

```python
def test_planner_loads_skill_then_returns_structured_plan() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    model = PlannerProbeModel()
    load_skill = create_load_skill_tool(root=repo_root)
    lodging_probe = make_read_probe_tool("lodging_search")
    agent = build_fast_agent(model, [load_skill, lodging_probe])

    result = asyncio.run(agent.ainvoke({
        "messages": [HumanMessage(content="travel-request-sentinel")],
        "execution_mode": "planning",
        "agent_phase": "planner",
    }, context=AssistantRunContext()))

    assert result["active_skill_ids"] == ["travel-tool-orchestration"]
    assert isinstance(result["structured_response"], NativePlanProposal)
    assert model.tool_sets[0] == {"load_skill"}
    assert "lodging_search" not in model.tool_sets[-1]
    assert [call.name for call in model.calls].count("load_skill") == 1
```

`PlannerProbeModel.bind_tools()` 必须记录标准化 Tool 名称，且只根据消息中的真实 `ToolMessage` 决定下一步；测试不得调用项目私有 graph node。

`probes.py` 中的业务 Tool 使用以下统一工厂，不连接真实 adapter：

```python
def make_read_probe_tool(name: str) -> BaseTool:
    @tool(name)
    def probe() -> str:
        """Return one deterministic experiment sentinel."""
        return f"{name}-result-sentinel"

    return probe.model_copy(update={"metadata": {"effect": "read"}})
```

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel-planning-experiment/test_planner_create_agent.py
```

Expected: FAIL，因为 fast agent 尚不根据 `agent_phase` 设置动态 response format，也未阻止 Planner 看到业务 Tool。

- [ ] **Step 3: 实现 phase middleware 的最小官方路径**

`PlanningPhaseMiddleware.wrap_model_call()` 只使用公开 `ModelRequest.override()`：

```python
class PlanningPhaseMiddleware(AgentMiddleware):
    def _project(self, request: ModelRequest) -> ModelRequest:
        phase = request.state.get("agent_phase", "fast")
        if phase == "planner":
            control_tools = [
                tool for tool in request.tools
                if _tool_name(tool) in {"load_skill", "load_skill_reference"}
            ]
            return request.override(
                tools=control_tools,
                response_format=ToolStrategy(NativePlanProposal),
            )
        if phase == "finalizer":
            return request.override(tools=[], response_format=None)
        return request
```

将 middleware 放在 `ProgressiveToolExposureMiddleware` 之后，使 Planner 能收到成功的 Skill state update，但最终 phase filter 始终移除业务 Tool。不得 import `langchain.agents.factory` 或 LangGraph 私有 node。

- [ ] **Step 4: 运行 G1 并记录决策**

Run: 使用 Step 2 命令。  
Expected: PASS，且 trace 中是标准 `model → load_skill ToolNode → model → structured_response`。

如果失败原因是 LangChain 1.3.15 不支持在同一 compiled agent 中动态切换 `response_format`，立即停止 Task 3-6；不要 monkeypatch graph。将结果记为 G1 failed，并修订规格为：

```text
build_agent_stack(...) 同时构造：
- planner_agent(response_format=NativePlanProposal, planner phase middleware)
- fast_agent(response_format=None, worker/finalizer phase middleware)
```

- [ ] **Step 5: 提交通过的 G1 spike**

```bash
git add src/assistant_agent/native_agent/planning_phase.py \
  src/assistant_agent/native_agent/fast_agent.py \
  tests/tdd/travel-planning-experiment/probes.py \
  tests/tdd/travel-planning-experiment/test_planner_create_agent.py
git commit -m "experiment: prove create-agent planner skill loop"
```

---

### Task 3: 验证 `Send` 的逐 Worker 能力投影与并行隔离（G2）

**Files:**
- Create: `src/assistant_agent/native_agent/planning_experiment.py`
- Create: `tests/tdd/travel-planning-experiment/test_worker_send_projection.py`

**Interfaces:**
- Consumes: Task 1 的 plan/state，Task 2 的 phase-aware shared agent。
- Produces: `ExperimentWorkerObservation`、`admit_experiment_plan()`、`project_worker_send()`、`build_travel_planning_experiment()`；只供实验测试和 Task 7 runner 使用。

- [ ] **Step 1: 写三路并行 Worker 的 RED 测试**

```python
def test_send_projects_distinct_worker_capabilities_without_leakage() -> None:
    plan = proposal(
        node("rail", tools=(), profile="rail_official"),
        node("hotel", tools=("lodging_search",), profile="none"),
        node(
            "route",
            tools=("mcp_amap_maps_maps_geo",),
            profile="none",
        ),
    )
    result = asyncio.run(run_experiment(plan))

    observed = {
        item.work_item_id: item for item in result["worker_observations"]
    }
    assert observed["rail"].visible_tool_names == ()
    assert observed["rail"].search_profile == "rail_official"
    assert observed["hotel"].visible_tool_names == ("lodging_search",)
    assert observed["route"].visible_tool_names == (
        "mcp_amap_maps_maps_geo",
    )
    assert worker_model.max_active_workers >= 2
    assert all(item.load_skill_calls == 0 for item in observed.values())
```

再添加 admission 断言：未激活 Skill、Tool 不属于 Skill manifest、未知 profile、重复 Tool 名都必须在创建 `Send` 前失败。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel-planning-experiment/test_worker_send_projection.py
```

Expected: FAIL，因为实验 Graph 和 Worker scope 尚不存在。

- [ ] **Step 3: 实现确定性 admission 与 `Send` 投影**

`project_worker_send()` 必须从受信 catalog 重新解析 Tool grant：

```python
def project_worker_send(
    node: NativePlanNode,
    *,
    activated_skill_ids: Collection[str],
    catalog: SkillCatalog,
    dependency_results: tuple[WorkerResult, ...],
) -> Send:
    skill_ids = admit_skill_subset(node.required_skill_ids, activated_skill_ids)
    descriptors = {item.name: item for item in catalog.descriptors}
    governed = {
        name
        for skill_id in skill_ids
        for name in descriptors[skill_id].governed_tools
    }
    requested = tuple(node.allowed_tool_names)
    if len(requested) != len(set(requested)) or not set(requested) <= governed:
        raise NativePlanAdmissionError("worker tool scope exceeds activated skills")
    return Send("worker", {
        "messages": [HumanMessage(content=node.objective)],
        "work_item_id": node.node_id,
        "dependency_results": dependency_results,
        "active_skill_ids": list(skill_ids),
        "skill_reference_grants": project_reference_grants(skill_ids, catalog),
        "worker_tool_allowlist": requested,
        "provider_search_profile": node.search_profile,
        "agent_phase": "worker",
        "execution_mode": "planning",
    })
```

Worker middleware 必须把所有 `request.tools` 与 `worker_tool_allowlist` 取交集；即使某 Tool 未被 Skill claim，也不得在 planning Worker 中绕过 allowlist。

实验 Graph 用独立 reducer 保存测试观察，不污染生产 `WorkerResult`：

```python
class ExperimentWorkerObservation(BaseModel):
    work_item_id: str
    visible_tool_names: tuple[str, ...]
    search_profile: ProviderSearchProfile
    load_skill_calls: int

class ExperimentPlanningState(PlanningState):
    worker_observations: Annotated[
        list[ExperimentWorkerObservation], operator.add
    ]
```

- [ ] **Step 4: 运行隔离与并行测试确认 GREEN**

Run: 使用 Step 2 命令。  
Expected: PASS；相同 compiled Worker graph 的三个 invocation state 不互相合并。

- [ ] **Step 5: 提交 G2 spike**

```bash
git add src/assistant_agent/native_agent/planning_experiment.py \
  tests/tdd/travel-planning-experiment/test_worker_send_projection.py
git commit -m "experiment: prove per-worker send capability isolation"
```

---

### Task 4: 验证 DashScope Profile payload 与 fail-closed 行为（G3）

**Files:**
- Modify: `src/assistant_agent/providers/dashscope_langchain.py`
- Modify: `tests/tdd/travel-planning-experiment/test_search_profile_payloads.py`

**Interfaces:**
- Consumes: `resolve_search_profile()`。
- Produces: `DashScopeNativeChatModel.bind(provider_search_profile=...)` 对六个枚举的显式 payload；`none` 能覆盖实例级 `enable_search=True`。

- [ ] **Step 1: 添加 fake transport 的 payload RED 测试**

```python
def test_none_profile_overrides_global_search_enablement() -> None:
    transport = SearchTransport()
    model = dashscope_model(transport, enable_search=True)
    model.bind(provider_search_profile="none").invoke("sentinel")
    assert "enable_search" not in transport.calls[0]["payload"]["parameters"]


@pytest.mark.parametrize(
    ("profile", "sites"),
    [
        ("rail_official", ["12306.cn"]),
        ("guide_xiaohongshu", ["xiaohongshu.com"]),
    ],
)
def test_strict_profiles_emit_assigned_sites(profile: str, sites: list[str]) -> None:
    transport = SearchTransport()
    model = dashscope_model(transport)
    model.bind(provider_search_profile=profile).invoke("sentinel")
    options = transport.calls[0]["payload"]["parameters"]["search_options"]
    assert options["search_strategy"] == "turbo"
    assert options["forced_search"] is True
    assert options["assigned_site_list"] == sites
    assert options["enable_source"] is True
```

未知 profile 和不支持协议的测试必须断言 `transport.calls == []`。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel-planning-experiment/test_search_profile_payloads.py
```

Expected: FAIL；当前 adapter 只有 `deep_research` 特例，且实例 `enable_search=True` 无法被 `none` 覆盖。

- [ ] **Step 3: 在 `_build_payload()` 中接入显式 policy**

规则必须是：

```python
profile = kwargs.get("provider_search_profile")
policy = (
    resolve_search_profile(profile, protocol="dashscope", model_name=self.model_name)
    if profile is not None
    else None
)
search_enabled = policy.enable_search if policy is not None else self.enable_search
```

当 `policy` 非空且启用时，`search_options` 只从 policy 机械生成；`assigned_site_list`、
`intention_options.prompt_intervene` 仅在 policy 对应字段非空时加入。保留现有 `deep_research` 兼容行为，但它不得被 planning schema 选择。

- [ ] **Step 4: 运行 payload 矩阵确认 GREEN**

Run: 使用 Step 2 命令。  
Expected: PASS，且 fake transport 不访问网络。

- [ ] **Step 5: 提交 G3 spike**

```bash
git add src/assistant_agent/providers/dashscope_langchain.py \
  tests/tdd/travel-planning-experiment/test_search_profile_payloads.py
git commit -m "experiment: validate dashscope travel search profiles"
```

---

### Task 5: 验证 Worker 来源穿透与 URL 不重建（G4）

**Files:**
- Modify: `src/assistant_agent/native_agent/models.py`
- Modify: `src/assistant_agent/native_agent/planning_experiment.py`
- Create: `tests/tdd/travel-planning-experiment/test_worker_sources.py`

**Interfaces:**
- Consumes: terminal `AIMessage.response_metadata.provider_search_sources`。
- Produces: `EvidenceLink`、扩展后的 `WorkerResult.sources`、`extract_worker_sources()`。

- [ ] **Step 1: 写来源归一化与并行归属 RED 测试**

```python
def test_worker_sources_are_mechanically_preserved_by_work_item() -> None:
    rail = AIMessage(
        content="rail-answer[1]",
        response_metadata={"provider_search_sources": [{
            "index": 1,
            "title": "12306-sentinel",
            "url": "https://www.12306.cn/sentinel",
        }]},
    )
    xhs = AIMessage(
        content="guide-answer[1]",
        response_metadata={"provider_search_sources": [{
            "index": 1,
            "title": "xhs-sentinel",
            "url": "https://www.xiaohongshu.com/sentinel",
        }]},
    )
    results = collect_probe_results({"rail": rail, "guide": xhs})
    assert results["rail"].sources[0].url.endswith("12306.cn/sentinel")
    assert results["guide"].sources[0].url.endswith("xiaohongshu.com/sentinel")
```

同时断言 `javascript:`、带 userinfo 的 URL、超过长度限制的 URL 被丢弃；正文里出现的 URL 若不在 metadata 中，不进入 `sources`。

- [ ] **Step 2: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel-planning-experiment/test_worker_sources.py
```

Expected: FAIL，因为 `WorkerResult` 当前只有 `content`。

- [ ] **Step 3: 实现有界来源模型和提取器**

```python
class EvidenceLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    index: int = Field(ge=1, le=1000)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2000)
    domain: str = Field(min_length=1, max_length=253)

class WorkerResult(BaseModel):
    # 保留 work_item_id/content
    verification_status: Literal["verified", "advisory", "unverified", "failed"]
    sources: tuple[EvidenceLink, ...] = ()
```

`extract_worker_sources()` 只读取 metadata list，使用 `urlsplit()` 接受无 userinfo 的 `https` URL，标准化 hostname，并最多保留 20 项。来源为空时，非 `none` profile 的 Worker 为 `unverified`；不得扫描正文生成链接。

- [ ] **Step 4: 运行 G4 确认 GREEN**

Run: 使用 Step 2 命令。  
Expected: PASS。

- [ ] **Step 5: 提交 G4 spike**

```bash
git add src/assistant_agent/native_agent/models.py \
  src/assistant_agent/native_agent/planning_experiment.py \
  tests/tdd/travel-planning-experiment/test_worker_sources.py
git commit -m "experiment: preserve planning worker search sources"
```

---

### Task 6: 运行完整 Mock 实验闭环

**Files:**
- Create: `tests/tdd/travel-planning-experiment/test_mock_experiment_graph.py`

**Interfaces:**
- Consumes: `build_travel_planning_experiment()`、Planner probe、Worker probe、profile policy、来源模型。
- Produces: 单个离线测试证明 G1-G4 能在同一 Graph run 内同时成立。

- [ ] **Step 1: 写完整旅行请求的 RED/GREEN 场景**

构造固定五节点计划：`rail`、`hotel`、`official_guide`、`xiaohongshu_guide` 并行，`route` 依赖前三项。断言：

```python
assert result["planner_load_skill_calls"] == 1
assert result["worker_load_skill_calls"] == 0
assert worker_model.max_active_workers >= 2
observed = {
    item.work_item_id: item for item in result["worker_observations"]
}
assert observed["rail"].visible_tool_names == ()
assert observed["hotel"].visible_tool_names == ("lodging_search",)
assert observed["route"].visible_tool_names == (
    "mcp_amap_maps_maps_geo",
    "mcp_amap_maps_maps_direction_transit_integrated",
)
assert by_id["rail"].sources[0].domain == "www.12306.cn"
assert by_id["xiaohongshu_guide"].sources[0].domain == "www.xiaohongshu.com"
assert isinstance(result["messages"][-1], AIMessage)
```

Finalizer probe 必须记录收到的 `WorkerResult.model_dump()`，并断言它没有 Tool、没有搜索 profile、没有新增来源 URL。

- [ ] **Step 2: 显式运行整个临时 TDD 目录**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/travel-planning-experiment
```

Expected: PASS；无网络访问。

- [ ] **Step 3: 运行现有 LOOP-001 定向安全网**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py
```

Expected: PASS；实验模块尚未替换生产 `planning_graph.py`，现有父图行为不变。

- [ ] **Step 4: 提交离线闭环证据**

```bash
git add tests/tdd/travel-planning-experiment/test_mock_experiment_graph.py
git commit -m "experiment: verify travel planning graph offline"
```

---

### Task 7: 增加并运行受门禁的 Qwen/DashScope 真实实验（G5）

**Files:**
- Create: `evals/system/travel_planning_profiles/__init__.py`
- Create: `evals/system/travel_planning_profiles/README.md`
- Create: `evals/system/travel_planning_profiles/runner.py`

**Interfaces:**
- Consumes: `validate_real_chat_config()`、`create_chat_model()`、`resolve_search_profile()`。
- Produces: `dry_run_report()`、`run_real_eval()`；只输出无正文、无凭据的结果 artifact。

- [ ] **Step 1: 编写 dry-run 与 real gate**

CLI：

```text
python -m evals.system.travel_planning_profiles.runner
  [--allow-real-provider]
  [--output-root PATH]
```

默认 dry-run 返回：

```json
{
  "status": "dry_run",
  "planned_provider_calls": 2,
  "profiles": ["rail_official", "guide_xiaohongshu"],
  "network_called": false
}
```

真实模式必须同时验证：

```python
if not allow_real_provider:
    raise SystemEvalConfigurationError("real provider not authorized")
validate_real_chat_config(config)
if config.chat_provider != "qwen" or config.qwen_chat_api_protocol != "dashscope":
    raise SystemEvalConfigurationError("travel profile eval requires qwen dashscope")
```

- [ ] **Step 2: 实现两例固定、非个人化查询**

仅调用：

```python
CASES = (
    ("rail_official", "查询中国铁路12306公开的京沪高铁车次信息，并引用来源。"),
    ("guide_xiaohongshu", "查询公开可索引的小红书上海旅行攻略，并引用来源。"),
)
```

artifact 每例只保存：

```python
{
    "profile": profile,
    "content_nonempty": bool(message.text.strip()),
    "source_count": len(sources),
    "source_domains": sorted(unique_domains),
    "allowed_domains": sorted(policy.assigned_site_list),
    "domain_subset_ok": all(domain_matches_allowlist(...)),
    "outcome": "passed" | "failed" | "inconclusive",
}
```

`source_count == 0` 时 outcome 必须为 `inconclusive`；不得把模型正文保存到 artifact 或控制台。

- [ ] **Step 3: 运行 dry-run**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m \
  evals.system.travel_planning_profiles.runner
```

Expected: `status=dry_run`、`planned_provider_calls=2`、`network_called=false`，不创建 artifact。

- [ ] **Step 4: 仅在 operator 明确授权后运行真实实验**

Run only with explicit operator approval:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m \
  evals.system.travel_planning_profiles.runner \
  --allow-real-provider
```

Expected: 恰好 2 次 Provider 调用；结果写入
`.data/evals/system/travel_planning_profiles/<run-id>/result.json`。任一来源域名越界则整体 failed；无来源为 inconclusive，不得宣称 G5 通过。

- [ ] **Step 5: 提交 runner，不提交 artifact**

```bash
git add evals/system/travel_planning_profiles
git commit -m "experiment: add gated travel search profile eval"
```

---

### Task 8: 审核实验结论并决定是否进入生产实施

**Files:**
- Modify only if evidence warrants: `docs/superpowers/specs/2026-08-19-travel-planning-search-profiles-design.md`
- Do not modify: `tests/core/**`

**Interfaces:**
- Consumes: G1-G5 结果、前七项任务的实验提交、可选 real artifact。
- Produces: 一项明确决策；不在本任务中迁移生产 `planning_graph.py`。

- [ ] **Step 1: 汇总 Gate 结果**

按以下固定表格记录在任务交付说明中：

```text
G1 same compiled create_agent planner: passed|failed
G2 per-worker Send isolation: passed|failed
G3 DashScope profile payloads: passed|failed
G4 source preservation: passed|failed
G5 real assigned-site behavior: passed|failed|inconclusive|not-run
```

- [ ] **Step 2: 应用决策规则**

```text
若 G1-G4 全部通过：允许编写生产实施计划；G5 可为 passed 或经用户接受的 inconclusive。
若 G1 失败：修订规格为 role-specific create_agent，不得继续“同一 compiled graph”实现。
若 G2 失败：终止生产迁移；不得使用全局 mutable state、ContextVar 或 worker 内重新 load_skill。
若 G3 失败：删除/禁用对应 profile，不运行真实 Provider。
若 G4 失败：先修订 WorkerResult/Finalizer 来源契约，不实现 App handoff。
若 G5 failed：禁止将严格站点 profile 标记为生产可用。
```

- [ ] **Step 3: 若结论改变设计，最小更新规格并提交**

只修改受到证据影响的段落和状态，不粘贴实验日志或 Provider 正文：

```bash
git add docs/superpowers/specs/2026-08-19-travel-planning-search-profiles-design.md
git commit -m "docs: record travel planning experiment decision"
```

若 G1-G4 全部按规格通过，则不制造无意义 spec diff。

- [ ] **Step 4: 最终汇报测试边界**

```text
Core invariant: unchanged during the isolated experiment. A later production
implementation must update LOOP-001 if Planner/Finalizer become phased uses of
the shared fast graph.

Tests: added tests/tdd/travel-planning-experiment for temporary RED/GREEN; the
user may delete the directory manually. Real Provider validation, if authorized,
ran only through evals/system/travel_planning_profiles and never through pytest.
```

列出实际运行过的命令、real 调用是否发生、调用次数、artifact 路径和所有未通过/未运行的 Gate。

---

## 计划自检

- Spec coverage：实验覆盖 Planner Skill loop、逐 Worker Skill/Tool/profile state、Provider profile payload、来源保留和真实站点限制；酒店、高德和 App 渲染属于低风险既有/后续生产迁移，不在 spike 中修改。
- Placeholder scan：每个代码步骤都给出了接口、断言、命令和失败动作，没有留待执行者自行猜测的内容。
- Type consistency：计划统一使用 `allowed_tool_names` 作为 plan 字段、`worker_tool_allowlist` 作为 Worker state 字段、`provider_search_profile` 作为模型请求字段。
- Test policy：实验 pytest 仅进入可删除的 `tests/tdd/travel-planning-experiment`；真实 Provider 仅进入正式 operator-gated system eval；core 只运行现有定向安全网，不在实验阶段修改。
