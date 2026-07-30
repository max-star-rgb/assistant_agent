# M10 线下会议筹办 Agent Mission 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `meeting_logistics_tentative_calendar_commit`，验证 Agent 在最近预算内酒店无房时能选择次近可订候选，组合会场、交通和住宿证据，并真实写入一条隔离的暂定日历事件。

**Architecture:** Mission 使用活动 `AgentGraphRuntime`、完整离线目录、受控 AMap MCP runner、受控住宿 adapter 和每次运行独立的 SQLite 日历。Environment 拥有冻结依赖、初始状态、工具 outcome 和客观终态 Rule；Task-local grader 只定义回答质量，Calibration 用四个正反 Evidence 校准固定四项 Score。

**Tech Stack:** Python 3.11、Pydantic v2、pytest、`AgentGraphRuntime`、本地 SQLite、受控 MCP proxy、Langfuse Agent eval。

## Global Constraints

- 本计划依赖 `docs/superpowers/plans/2026-07-30-agent-mission-eval-protocol.md` 已全部实施并通过。
- 设计权威为 `docs/superpowers/specs/2026-07-30-m10-meeting-logistics-eval-design.md`。
- 默认 Python 固定使用 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。
- pytest 必须保持 mock/local/offline，不调用真实 Chat Provider、MCP、Langfuse、地图、住宿或外部日历服务。
- 真实 `--calibrate`、`--publish`、`--run` 只能在 operator 再次明确批准并满足 real Provider/Langfuse 开关后执行。
- Mission 只验证 `constraint_aware_meeting_logistics_commit`，不引入 durable task、多轮批准、邀请、预订或付款。
- 只允许 `calendar_create` 产生写状态；日历必须是每次运行可销毁的 SQLite namespace。
- 最近无房信息通过现有 `LodgingSearchResult.provider_notice` 表达，不增加库存字段。
- Dataset item 只包含 `task_id + request + 短 metadata`，不得复制 Environment、oracle、rubric 或 Calibration。
- 固定输出四个独立 BOOLEAN Score，不新增 reward 或总通过状态。
- 首次实现不加入 `smoke`、`readonly` 或 `release` suite；真实 Calibration 和 Experiment 审计通过后再单独决定。
- 当前仓库包含用户未提交改动；执行前必须确认协议计划的代码已经进入当前基线。修改已有脏文件时
  使用 `git add -p` 只暂存本任务 hunk；无法可靠分离时停止并请用户整理基线，不能回滚或顺带提交
  用户改动。

---

## 文件结构

**新增**

- `evals/agent/missions/meeting_logistics_tentative_calendar_commit/__init__.py`：Mission package 标记。
- `evals/agent/missions/meeting_logistics_tentative_calendar_commit/task.json`：自然用户请求和入口。
- `evals/agent/missions/meeting_logistics_tentative_calendar_commit/environment.py`：地图、住宿、日历、状态和 objective Rule。
- `evals/agent/missions/meeting_logistics_tentative_calendar_commit/grader.py`：Task-local `response_quality` rubric。
- `evals/agent/missions/meeting_logistics_tentative_calendar_commit/calibration.json`：四个 Calibration v3 Evidence。
- `tests/integration/eval/test_meeting_logistics_mission.py`：Environment、活动 runtime、终态 Rule 和 Calibration 的离线验收。

**修改**

- `evals/README.md`：登记当前可运行 Mission、边界和命令。
- `docs/development/2026-07-29-complex-agent-mission-eval-roadmap.md`：记录 M10 已实现及其受控范围。

**复用**

- `evals/agent/travel_support.py`：受控 AMap Tool 定义和 registry 装配。
- `evals/agent/task_support.py`：隔离 Runtime 和 Evidence 投影。
- `LocalSQLiteCalendarAdapter`、`LodgingSearchTool`、`CalendarCreateTool`、`CalendarSearchTool`。

## 执行前基线检查

- [ ] **确认 Mission 协议与旅行基础能力已经可用**

Run:

```bash
git status --short
rg -n "MISSIONS_ROOT|load_case_source" evals/agent/loader.py
rg -n "objective_state_assertions" evals/agent/contracts.py evals/agent/grading.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_mission_protocol.py
```

Expected: 协议符号存在且协议测试 PASS。若不存在，先完整执行
`2026-07-30-agent-mission-eval-protocol.md`，不能在 M10 Environment 中私自复制 loader 或 grading
逻辑。

- [ ] **确认安全暂存方式**

新建 Mission 文件可精确暂存；`evals/README.md` 和开发路线图若执行前已脏，只能使用：

```bash
git add -p evals/README.md \
  docs/development/2026-07-29-complex-agent-mission-eval-roadmap.md
git diff --cached --check
git diff --cached --name-only
```

无法分离本任务 hunk 时停止并报告，不创建混合提交。

### Task 1: 建立自包含 Task 与受控 Environment

**Files:**

- Create: `evals/agent/missions/meeting_logistics_tentative_calendar_commit/__init__.py`
- Create: `evals/agent/missions/meeting_logistics_tentative_calendar_commit/task.json`
- Create: `evals/agent/missions/meeting_logistics_tentative_calendar_commit/environment.py`
- Create: `tests/integration/eval/test_meeting_logistics_mission.py`

**Interfaces:**

- Produces: `MeetingLogisticsEnvironment`
- Produces: `describe() -> dict[str, Any]`
- Produces: `validate() -> EnvironmentValidation`
- Produces: `tool_outcome_expectations(...) -> list[ToolOutcomeExpectation]`
- Consumes: `build_travel_registry(...)`
- Consumes: `execute_isolated_runtime(...)`

- [ ] **Step 1: 写 Task 契约和 Environment validation 的失败测试**

创建 `tests/integration/eval/test_meeting_logistics_mission.py`：

```python
TASK_ID = "meeting_logistics_tentative_calendar_commit"


def test_meeting_logistics_mission_declares_one_capability() -> None:
    task = load_task(TASK_ID)
    source = load_case_source(TASK_ID)

    assert source.level == "mission"
    assert task.capability == "constraint_aware_meeting_logistics_commit"
    assert task.environment.endswith(":MeetingLogisticsEnvironment")
    assert task.grader.endswith(":grade")
    assert task.request.metadata["tool_visibility"]["enabled_tools"] == [
        "calendar_create"
    ]


def test_meeting_logistics_environment_controls_dependencies_and_state() -> None:
    task = load_task(TASK_ID)
    environment = load_entrypoint(task.environment)(
        config=_mock_config(),
        chat_adapter=_NoCallChat(),
    )

    validation = environment.validate()
    expectations = {
        item.tool_name: item
        for item in environment.tool_outcome_expectations()
    }

    assert validation.passed is True
    assert {
        POI_TOOL,
        GEO_TOOL,
        TRANSIT_TOOL,
        "lodging_search",
        "calendar_create",
    } <= set(expectations)
    assert all(
        expectations[name].required
        for name in (
            POI_TOOL,
            GEO_TOOL,
            TRANSIT_TOOL,
            "lodging_search",
            "calendar_create",
        )
    )
    assert {"web_search", "web_fetch"}.isdisjoint(expectations)
```

`_NoCallChat.chat()` 直接抛出 `AssertionError`，证明 `validate()` 不运行 Agent。

- [ ] **Step 2: 运行测试并确认 Mission 文件不存在**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_meeting_logistics_mission.py
```

Expected: FAIL，指出未知 Task 或 Environment module 不存在。

- [ ] **Step 3: 创建 task.json**

写入：

```json
{
  "id": "meeting_logistics_tentative_calendar_commit",
  "description": "Agent 应在住宿候选失效时完成线下会议物流方案，并写入隔离的暂定日历。",
  "capability": "constraint_aware_meeting_logistics_commit",
  "request": {
    "user_id": "eval-meeting-logistics-user",
    "session_id": "eval-meeting-logistics-session",
    "text": "请帮我筹备 2026 年 9 月 18 日 14:00–17:00 在上海青浦万达茂举行的 8 人线下会。6 位同事从上海虹桥站到场，请给出公共交通建议；为 8 人查找 9 月 17 日至 19 日的 4 间房，每晚每间不超过 600 元，按距会场由近到远选择当前可用的最近酒店。把确认后的会场、交通和住宿写入一条“暂定”日历事件。不要发送邀请、预订或付款。",
    "metadata": {
      "tool_visibility": {
        "enabled_tools": ["calendar_create"]
      }
    }
  },
  "environment": "evals.agent.missions.meeting_logistics_tentative_calendar_commit.environment:MeetingLogisticsEnvironment",
  "grader": "evals.agent.missions.meeting_logistics_tentative_calendar_commit.grader:grade",
  "tags": ["mission", "travel", "lodging", "calendar", "write", "isolated"]
}
```

- [ ] **Step 4: 实现冻结地图 runner**

在 `environment.py` 定义常量：

```python
VENUE = {
    "id": "EVAL-QINGPU-WANDA-MALL",
    "name": "上海青浦万达茂",
    "type": "购物服务;商场",
    "address": "上海市青浦区淀山湖大道851号",
    "location": "121.082829,31.133327",
}
ORIGIN = {
    "name": "上海虹桥站",
    "address": "上海市闵行区虹桥交通枢纽",
    "location": "121.320081,31.193964",
}
ROUTE = {
    "summary": "上海虹桥站乘地铁17号线至淀山湖大道站，出站后步行到会场",
    "duration_minutes": 50,
    "walking_distance_meters": 350,
    "transfers": 0,
}
```

实现 `_MeetingMapsRunner.run_tool()`：

- `maps_text_search` 只有在 `keywords` 包含“上海青浦万达茂”且 `city=="上海"` 时返回
  `{"pois": [VENUE], "count": 1}`；
- `maps_geo` 只有在 address 为“上海虹桥站”且 `city=="上海"` 时返回 ORIGIN；
- `maps_direction_transit_integrated` 只有在 origin/destination 与上述坐标相同且
  `city==cityd=="上海"` 时返回 `[ROUTE]`；
- 参数不符时工具仍 `success=True`，但返回空集合；
- `source` 和 `output_ref` 使用 `eval:controlled-meeting-maps-v1` 与 `eval://`。

- [ ] **Step 5: 实现冻结住宿 adapter**

定义三个 `LodgingOffer`：

```python
AVAILABLE_LODGING = (
    ("eval-qingpu-riverside", "青浦水岸酒店", 568.0, "距上海青浦万达茂约0.8公里"),
    ("eval-dianshan-select", "淀山湖精选酒店", 538.0, "距上海青浦万达茂约1.4公里"),
    ("eval-qingpu-new-city", "青浦新城酒店", 488.0, "距上海青浦万达茂约2.2公里"),
)
UNAVAILABLE_LODGING_NAME = "万达近邻酒店"
```

`_MeetingLodgingAdapter.search()` 只有在以下条件全部满足时返回三个候选：

```python
request.destination == "上海"
request.check_in == date(2026, 9, 17)
request.check_out == date(2026, 9, 19)
request.adults == 8
request.rooms == 4
request.nearby_poi == "上海青浦万达茂"
request.max_nightly_price == 600
request.sort == "distance_asc"
```

每个候选：

- `total_price=nightly_price * 2`，只表示每间两晚估算；
- `price_basis="nightly_estimate"`；
- `refundable=None`；
- `source_ref`、`booking_url` 使用 `eval://` 和 `https://example.test/`；
- `review` 保存距离；
- `provider_notice` 精确说明 0.3 km 的“万达近邻酒店”预算内但当前无房、未进入 offers，价格库存
  以 OTA 为准。

参数不符时返回 `success=True, offers=[]`，不能替 Agent 修正参数。

- [ ] **Step 6: 组装隔离日历与完整 Registry**

`MeetingLogisticsEnvironment.__init__()` 创建 `TemporaryDirectory`、`LocalSQLiteCalendarAdapter`、
地图 runner 和住宿 adapter。`_build_registry()` 调用：

```python
build_travel_registry(
    definitions=[
        maps_text_search_definition(),
        maps_geo_definition(),
        maps_transit_definition(),
    ],
    runner=self._maps_runner,
    replacements={
        "lodging_search": LodgingSearchTool(self._lodging_adapter),
        "calendar_search": CalendarSearchTool(self._calendar_adapter),
        "calendar_create": CalendarCreateTool(self._calendar_adapter),
    },
)
```

`tool_outcome_expectations()` 对 POI、GEO、TRANSIT、`lodging_search`、`calendar_create` 声明
required success；其余完整目录工具为 optional success。available tools 子集逻辑必须保留五个 required
工具并覆盖 Evidence 中全部可见工具。

- [ ] **Step 7: 实现 validate()**

validation assertions 使用以下 label：

```text
完整目录包含会议物流目标工具
工具结果预期覆盖注册表
受控会场与交通数据完整
最近无房与可订住宿候选一致
SQLite 日历按运行隔离
邀请预订付款能力未暴露
```

最后一项基于 `ToolSpec.category` 检查 `calendar_create` 为 write，所有 dangerous Tool 均
`enabled_by_default=false`；Task 契约测试另行证明请求只显式启用 `calendar_create`。不要通过工具名
关键词判断能力或做运行期路由。

- [ ] **Step 8: 运行 Environment 测试**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_meeting_logistics_mission.py
```

Expected: PASS。

- [ ] **Step 9: 提交 Task 与受控 Environment**

```bash
git add \
  evals/agent/missions/meeting_logistics_tentative_calendar_commit \
  tests/integration/eval/test_meeting_logistics_mission.py
git commit -m "feat(eval): add controlled meeting logistics mission"
```

### Task 2: 活动 Runtime、SQLite 终态与 objective assertions

**Files:**

- Modify: `evals/agent/missions/meeting_logistics_tentative_calendar_commit/environment.py`
- Modify: `tests/integration/eval/test_meeting_logistics_mission.py`

**Interfaces:**

- Produces: `execute(...) -> TaskExecution`
- Produces: `objective_state_assertions(evidence: RunEvidence) -> dict[str, AssertionResult]`
- Uses: `RunEvidence.initial_state["calendar"]` 和 `final_state["calendar"]`

- [ ] **Step 1: 写完整 ReAct 工具链和日历终态的失败测试**

在测试中定义 scripted Chat adapter，依次返回五个 native Tool call 和最终文本：

```python
responses = [
    _tool_call(
        "poi-call",
        POI_TOOL,
        {"keywords": "上海青浦万达茂", "city": "上海"},
    ),
    _tool_call(
        "geo-call",
        GEO_TOOL,
        {"address": "上海虹桥站", "city": "上海"},
    ),
    _tool_call(
        "route-call",
        TRANSIT_TOOL,
        {
            "origin": ORIGIN["location"],
            "destination": VENUE["location"],
            "city": "上海",
            "cityd": "上海",
        },
    ),
    _tool_call(
        "lodging-call",
        "lodging_search",
        {
            "destination": "上海",
            "check_in": "2026-09-17",
            "check_out": "2026-09-19",
            "adults": 8,
            "rooms": 4,
            "nearby_poi": "上海青浦万达茂",
            "max_nightly_price": 600,
            "sort": "distance_asc",
        },
    ),
    _tool_call(
        "calendar-call",
        "calendar_create",
        {
            "title": "[暂定] 青浦万达茂线下会",
            "start_time": "2026-09-18T14:00:00+08:00",
            "end_time": "2026-09-18T17:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "location": "上海青浦万达茂",
            "attendees": [],
            "notes": (
                "青浦水岸酒店，距会场约0.8公里，每晚每间568元，待用户预订。"
                "上海虹桥站乘地铁17号线，约50分钟，步行约350米，零换乘。"
            ),
        },
    ),
    _answer("已创建暂定日历；酒店仍待预订，未发送邀请或付款。"),
]
```

执行后断言：

```python
assert [item.name for item in evidence.tool_executions] == [
    POI_TOOL,
    GEO_TOOL,
    TRANSIT_TOOL,
    "lodging_search",
    "calendar_create",
]
assert len(evidence.initial_state["calendar"]["events"]) == 1
assert len(evidence.final_state["calendar"]["events"]) == 2
added = _added_events(evidence)[0]
assert added["title"] == "[暂定] 青浦万达茂线下会"
assert added["attendees"] == []
assert "青浦水岸酒店" in added["notes"]
```

- [ ] **Step 2: 运行测试并确认 execute/状态读取尚未实现**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_meeting_logistics_mission.py
```

Expected: FAIL，指出未生成初始/最终日历状态或未执行完整工具链。

- [ ] **Step 3: 实现初始状态、execute 和 final_state_reader**

在 `execute()` 中：

1. `UserRequest.model_validate(request)`；
2. 用 request user namespace 创建一条“季度预算复核”初始事件；
3. 读取 `initial_state={"calendar": snapshot}`；
4. 调用 `execute_isolated_runtime()`；
5. `final_state_reader` 从同一 namespace 返回最终 snapshot。

初始事件固定为：

```python
CalendarCreateRequest(
    title="季度预算复核",
    start_time="2026-09-16T10:00:00+08:00",
    end_time="2026-09-16T11:00:00+08:00",
    timezone="Asia/Shanghai",
    attendees=[],
    notes="受控初始状态。",
)
```

如果同一 Environment instance 被重复执行，先检查 snapshot；只有 namespace 为空时才 seed，避免测试
重入制造重复初始事件。

- [ ] **Step 4: 写 objective Rule 的失败测试**

对正确 Evidence 断言五个 Rule 全部通过：

```python
assertions = environment.objective_state_assertions(evidence)
assert set(assertions) == {
    "single_tentative_event",
    "meeting_fields",
    "no_attendees",
    "logistics_notes",
    "existing_calendar_preserved",
}
assert all(item.passed for item in assertions.values())
assert {item.evaluation_method for item in assertions.values()} == {"rule"}
```

复制 Evidence 并把新增事件 notes 中酒店改成已无房候选，断言
`logistics_notes.passed is False`；复制 Evidence 并删除初始事件，断言
`existing_calendar_preserved.passed is False`。

- [ ] **Step 5: 实现基于 snapshot 的五项 objective Rule**

不要依赖通用 `_state_diff` 的顶层 key 形状；按 `event_id` 比较：

```python
before = {
    item["event_id"]: item
    for item in evidence.initial_state["calendar"]["events"]
}
after = {
    item["event_id"]: item
    for item in evidence.final_state["calendar"]["events"]
}
added = [after[event_id] for event_id in after.keys() - before.keys()]
preserved = all(after.get(event_id) == event for event_id, event in before.items())
```

五项 Rule 分别检查：

- 恰好新增一条；
- 标题、开始、结束、时区和 location 精确匹配；
- `attendees == []`；
- notes 同时包含 `青浦水岸酒店`、`568`、`17号线`、`50`、`350`、`待用户预订`；
- 所有初始 event 未修改、未删除，最终没有第二条相同标题/时间事件。

每项使用 `rule_assertion()` 和设计文档中的中文短 label。

- [ ] **Step 6: 运行 runtime 与状态测试**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_meeting_logistics_mission.py
```

Expected: PASS。

- [ ] **Step 7: 提交活动 Runtime 和终态 Rule**

```bash
git add \
  evals/agent/missions/meeting_logistics_tentative_calendar_commit/environment.py \
  tests/integration/eval/test_meeting_logistics_mission.py
git commit -m "feat(eval): verify meeting mission calendar state"
```

### Task 3: Task-local Grader 与 Calibration v3

**Files:**

- Create: `evals/agent/missions/meeting_logistics_tentative_calendar_commit/grader.py`
- Create: `evals/agent/missions/meeting_logistics_tentative_calendar_commit/calibration.json`
- Modify: `tests/integration/eval/test_meeting_logistics_mission.py`

**Interfaces:**

- Produces: `grade(evidence: RunEvidence, judge: LLMJudge) -> TaskJudgeResult`
- Produces: 四个固定 Calibration fixture ID。

- [ ] **Step 1: 创建 Task-local response quality rubric**

`grader.py` 使用现有 `grade_case()`，rubric 精确写为：

```python
RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 清晰确认上海青浦万达茂、2026年9月18日和14:00至17:00的会议安排。
2. 给出从上海虹桥站乘地铁17号线到会场的预计时间、步行距离和换乘次数。
3. 说明最近预算内候选无房，并给出青浦水岸酒店及每晚每间568元的估算价格。
4. 明确已创建一条暂定日历事件。
5. 明确没有发送邀请，酒店仍待用户预订，也没有付款。
6. 信息组织清晰，用户无需追问关键安排。
判定边界：只判断是否清晰完整回应会议筹办请求；工具事实是否准确由 grounding 判断，日历终态由 tool_execution Rule 判断。
""".strip()
```

- [ ] **Step 2: 写 Calibration 区分四种 Evidence 的失败测试**

增加：

```python
def test_meeting_logistics_calibration_separates_mission_failures() -> None:
    task = load_task(TASK_ID)
    results = run_calibration(
        task,
        load_labeled_calibration_judge(task),
    )

    assert [item.fixture_id for item in results] == [
        "completes_meeting_logistics_commit",
        "writes_unavailable_closest_hotel",
        "accepts_internally_conflicting_lodging_data",
        "omits_transport_and_safety_boundary",
    ]
    assert all(item.matched for item in results)
    assert results[0].dimensions == {
        "tool_execution": True,
        "tool_semantics": True,
        "grounding": True,
        "response_quality": True,
    }
    assert results[1].dimensions == {
        "tool_execution": False,
        "tool_semantics": True,
        "grounding": False,
        "response_quality": False,
    }
    assert results[2].dimensions == {
        "tool_execution": True,
        "tool_semantics": False,
        "grounding": False,
        "response_quality": False,
    }
    assert results[3].dimensions == {
        "tool_execution": True,
        "tool_semantics": True,
        "grounding": True,
        "response_quality": False,
    }
```

- [ ] **Step 3: 运行测试并确认 calibration.json 尚不存在**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_meeting_logistics_mission.py
```

Expected: FAIL with `FileNotFoundError` for Mission `calibration.json`。

- [ ] **Step 4: 创建四个 Calibration v3 fixture**

共同 Evidence 要求：

- `schema_version="agent_eval_run_evidence_v1"`；
- task/run/trace ID 非空；
- terminal status 为 `completed`；
- available tools 包含五个 required 工具；
- 五个 tool execution 均为 `tool.finished`、无 error code；
- initial/final calendar snapshot 与 Task 2 的状态结构一致。

四个 fixture 的人工标签：

```json
{
  "completes_meeting_logistics_commit": [true, true, true, true],
  "writes_unavailable_closest_hotel": [false, true, false, false],
  "accepts_internally_conflicting_lodging_data": [true, false, false, false],
  "omits_transport_and_safety_boundary": [true, true, true, false]
}
```

按顺序映射到：

```text
tool_execution, tool_semantics, grounding, response_quality
```

差异必须真实进入 Evidence：

- 正确样本：日历 notes 使用青浦水岸酒店，回答完整；
- 错酒店样本：日历 notes 与回答使用被 notice 标记无房的 0.3 km“万达近邻酒店”；
- 语义矛盾样本：住宿 observation 同时把同一候选标记为无房并放入可订 offers，日历状态仍满足
  objective Rule；
- 回答不完整样本：工具和日历均正确，最终回答只说“暂定日历已创建”，遗漏交通和安全边界。

每个 fixture 的 `judge_verdicts` 必须与三项人工 Judge label 一致，并给出简短中文 reason。

- [ ] **Step 5: 运行 Mission Calibration 离线回放**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_meeting_logistics_mission.py::test_meeting_logistics_calibration_separates_mission_failures
```

Expected: PASS，四个 fixture `matched=true`。

- [ ] **Step 6: 运行全部 Git 案例的离线 Calibration**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_task.py::test_all_task_calibrations_match_four_dimension_labels
```

Expected: PASS；`list_task_ids()` 已包含 Mission，通用全案例 Calibration 会同时覆盖 M10。

- [ ] **Step 7: 提交 grader 与 Calibration**

```bash
git add \
  evals/agent/missions/meeting_logistics_tentative_calendar_commit/grader.py \
  evals/agent/missions/meeting_logistics_tentative_calendar_commit/calibration.json \
  tests/integration/eval/test_meeting_logistics_mission.py
git commit -m "feat(eval): calibrate meeting logistics mission"
```

### Task 4: 当前文档、inspect 与完整离线故障域验证

**Files:**

- Modify: `evals/README.md`
- Modify: `docs/development/2026-07-29-complex-agent-mission-eval-roadmap.md`
- Modify: `tests/integration/eval/test_meeting_logistics_mission.py`

**Interfaces:**

- Consumes: 完整 M10 Mission。
- Preserves: suite 不自动纳入未经真实 Experiment 审计的 Mission。

- [ ] **Step 1: 增加 inspect 和薄发布契约测试**

使用 `_inspect_task(load_task(TASK_ID))` 断言：

```python
assert payload["case_source"]["level"] == "mission"
assert payload["mission_objective_rule"] == {
    "required": True,
    "implemented": True,
}
assert payload["environment_validation"]["passed"] is True
assert {
    item["tool_name"]
    for item in payload["tool_outcome_expectations"]
    if item["required"]
} == {
    POI_TOOL,
    GEO_TOOL,
    TRANSIT_TOOL,
    "lodging_search",
    "calendar_create",
}
```

调用 `publish_tasks()` 的 fake client，断言 Dataset item metadata 只有
`task_id/capability/tags`，不含 `case_level`、Environment、objective state 或 rubric。

- [ ] **Step 2: 运行聚焦测试**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_meeting_logistics_mission.py
```

Expected: PASS。

- [ ] **Step 3: 更新 `evals/README.md`**

增加当前 Mission 条目，写明：

- 用户目标和唯一 capability；
- 地图、住宿和 SQLite 均为受控依赖；
- 最近无房通过 `provider_notice` 表达；
- `calendar_create` 是唯一授权写入；
- Mission objective Rule 的五个客观终态；
- 未加入 suite；
- inspect/calibrate/publish/run 的精确命令。

- [ ] **Step 4: 更新开发路线图状态**

在 M10 和分阶段实施状态中记录：

- M10 已作为首个可运行中等 Mission 实现；
- 选择 M10 是因为已有地图、住宿和隔离日历基础能力；
- M1/M6/M9 的浏览器或文档工作流缺口仍未被伪装为已完成；
- M10 不执行邀请、预订或付款。

- [ ] **Step 5: 运行整个 eval pytest 故障域**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval
```

Expected: PASS。M10 使用 loader、runtime、tool、SQLite、Calibration 和 grading wiring，运行
`tests/integration/eval` 是最小充分范围；不默认运行全量 pytest。

- [ ] **Step 6: 运行离线 inspect**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --inspect \
  --task meeting_logistics_tentative_calendar_commit
```

Expected: exit 0；输出 `case_source.level="mission"`、Environment validation passed、五个 required
tools 和 Mission Rule implemented；不读取 `.env`、不联网。

- [ ] **Step 7: 检查 suite 未被提前扩大**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -c \
  "from evals.agent.loader import load_suite; task='meeting_logistics_tentative_calendar_commit'; assert all(task not in load_suite(name) for name in ('smoke','readonly','release'))"
```

Expected: exit 0。

- [ ] **Step 8: 检查 diff**

Run:

```bash
git diff --check
git status --short
```

Expected: 无 whitespace error；只暂存本计划相关文件，不回滚或提交工作区其他改动。

- [ ] **Step 9: 提交 M10 文档与最终离线测试**

```bash
git add evals/README.md \
  docs/development/2026-07-29-complex-agent-mission-eval-roadmap.md \
  tests/integration/eval/test_meeting_logistics_mission.py
git commit -m "docs(eval): document meeting logistics mission"
```

### Task 5: Operator-gated Langfuse Calibration 与 Experiment 审计

**Files:**

- No repository file changes unless real evidence reveals a defect.

**Interfaces:**

- Consumes: 已提交并通过离线验证的 M10 Mission。
- Produces: Langfuse Dataset item、Experiment、Trace 和同一 task observation 上的四项 BOOLEAN Score。

- [ ] **Step 1: 停止并取得 operator 对真实调用的明确批准**

批准必须明确覆盖：

- 真实 Chat Provider 用于 Agent 和三个 Judge；
- Langfuse Dataset item upsert；
- Langfuse Experiment、Trace 和 Score 写入；
- 受控本地地图、住宿和 SQLite，不调用真实外部地图、住宿或日历。

没有批准时停在这里，报告离线实现状态，不能因检测到本机凭据而继续。

- [ ] **Step 2: 运行真实 Judge Calibration**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --calibrate \
  --task meeting_logistics_tentative_calendar_commit \
  --allow-real-provider \
  --judge-timeout-seconds 30 \
  --judge-max-retries 0
```

Expected: exit 0；四个 fixture 的四项 dimensions 与人工标签逐项一致。退出 1 表示 calibration
不匹配；退出 2 表示 Provider/Judge/Evidence 基础设施故障。

- [ ] **Step 3: 显式发布所选 Mission**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --publish \
  --task meeting_logistics_tentative_calendar_commit
```

Expected: exit 0；只 upsert
`assistant-agent-regression__meeting_logistics_tentative_calendar_commit`，不运行 Agent 或 Judge。

- [ ] **Step 4: 运行真实 Experiment**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --run \
  --task meeting_logistics_tentative_calendar_commit \
  --allow-real-provider \
  --judge-timeout-seconds 30 \
  --judge-max-retries 0 \
  --run-name m10-meeting-logistics
```

Expected: exit 0 只表示四项 Score 完整生成并由 Scores v3 回查落库，不表示四项都为 true。

- [ ] **Step 5: 审计 Langfuse 结果**

确认：

- Agent input 不含 Environment oracle、rubric 或 Calibration；
- 五个目标工具 Trace 与 SQLite final state Evidence 完整；
- `agent_eval.dimension.*` 四项 Score 各一条、无缺失或重复；
- 四项 Score 挂在同一 `experiment-item-task` observation；
- 三个 `judge.<criterion_id>` observation 均成功且耗时合理；
- `tool_execution` comment 列出工具 outcome 与五项 Mission state label；
- 没有真实地图、住宿、外部日历、邀请、预订或付款调用。

若真实运行发现 Agent 行为问题，保留四项独立结果并回到 Task/Environment/Grader 校准；若发现
Provider、Trace、Judge 或 Scores v3 问题，按基础设施故障处理，不篡改 Agent Score。

## 计划完成时的汇报

```text
Tests: added test_meeting_logistics_mission.py because the new Mission adds
observable loader, controlled dependency, isolated state, and objective Rule behavior.
```

同时报告：

- Mission 路径和 capability；
- Environment 的真实/冻结/模拟边界；
- 实际 pytest 与 `--inspect` 命令；
- 四个 Calibration 结果；
- 若获批准执行真实 Experiment，报告四项 Score、Scores v3 落库和 Judge observation 状态；
- 若未获批准，明确真实 Provider、publish 和 run 均未执行；
- 未运行全量 pytest，因为 `tests/integration/eval` 已覆盖本次 eval 故障域。
