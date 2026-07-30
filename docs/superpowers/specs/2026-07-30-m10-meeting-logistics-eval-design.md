# M10 线下会议筹办 Agent Mission 设计

状态：设计已逐节批准，待用户复核文档

日期：2026-07-30

适用项目：`assistant_agent`

> 本文是实施前设计，不是当前架构事实权威。当前评测协议仍以
> `evals/README.md`、源码和测试为准。实现完成后应把稳定契约同步回权威文档。

## 1. 背景与目标

现有 Agent eval 已覆盖地点消歧、公共交通证据链、住宿约束、隔离日历写入等基础能力，但尚未证明
Agent 能把这些能力组合为一个有限、可恢复且具有客观终态的用户目标。

本设计选择路线图中的 M10“线下会议筹办助手”，以一次明确授权的单轮 Mission 验证：

> Agent 能否在住宿候选发生失效时，综合会场、交通和住宿证据形成可执行方案，并把结果写入一条
> 可逆、可审计的暂定日历事件，同时保持邀请、预订和付款边界。

该 Mission 的唯一 capability 为：

```text
constraint_aware_meeting_logistics_commit
```

“一个 capability”在这里表示一个完整用户目标，不表示只能调用一个工具。

## 2. 已批准的范围

### 2.1 Task 标识

```text
task_id: meeting_logistics_tentative_calendar_commit
case_level: mission
location: evals/agent/missions/meeting_logistics_tentative_calendar_commit/
```

`case_level` 由 loader 根据来源目录确定，不进入 `task.json`，也不依赖 tag 或名称猜测。

### 2.2 用户请求

```text
请帮我筹备 2026 年 9 月 18 日 14:00–17:00 在上海青浦万达茂举行的
8 人线下会。6 位同事从上海虹桥站到场，请给出公共交通建议；为 8 人查找
9 月 17 日至 19 日的 4 间房，每晚每间不超过 600 元，按距会场由近到远
选择当前可用的最近酒店。把确认后的会场、交通和住宿写入一条“暂定”
日历事件。不要发送邀请、预订或付款。
```

请求只描述正常用户目标，不披露 Environment 中的无房注入、正确候选、终态 oracle 或 grader
rubric。用户在同一请求中明确授权创建暂定日历，因此本 Mission 不增加第二轮人工确认协议。

### 2.3 非目标

- 不比较或预订多个会场；
- 不发送群体邀请；
- 不预订场地或酒店；
- 不付款、不接受协议、不处理 OTP 或 CAPTCHA；
- 不持续监控库存；
- 不引入 durable task、多轮用户事件或新的 Agent runtime；
- 不调用真实地图、酒店或外部日历服务。

## 3. Environment

### 3.1 运行边界

Environment 使用活动 `AgentGraphRuntime` 和正常 assistant loop。主 Agent 与 Judge 仍使用 operator
显式启用的真实 Chat Provider；地图、住宿和日历依赖全部为本地受控实现。

每次运行创建独立临时目录和 SQLite 日历，运行结束后销毁。Agent 看不到 Environment oracle、
Mission 终态 Rule、校准标签或 Judge rubric。

### 3.2 工具目录

目标工具为：

1. `mcp.amap_maps.maps_text_search`：确认上海青浦万达茂 POI；
2. `mcp.amap_maps.maps_geo`：解析上海虹桥站；
3. `mcp.amap_maps.maps_direction_transit_integrated`：规划公共交通；
4. `lodging_search`：查询住宿候选；
5. `calendar_create`：写入暂定事件。

Environment 保持默认完整离线工具目录的选择压力，并加入受控 AMap MCP proxy；本地
`web_search`、`web_fetch` 不注册。`calendar_create` 通过请求 metadata 中的结构化
`tool_visibility.enabled_tools` 显式授权，不通过请求文本或关键词推断。

如果真实 Qwen Chat Provider 在 `llm.chat` 内部使用 Provider-native 联网，它不形成本地工具调用，
也不能替代受控地图、住宿和日历 Evidence。最终评分只以 Mission 的工具轨迹、状态和回答为依据。

### 3.3 冻结地图数据

受控地点基线为：

| 地点 | 地址 | 坐标，`经度,纬度` |
| --- | --- | --- |
| 上海青浦万达茂 | 上海市青浦区淀山湖大道 851 号 | `121.082829,31.133327` |
| 上海虹桥站 | 上海市闵行区虹桥交通枢纽 | `121.320081,31.193964` |

公共交通结果固定为：

```text
summary: 上海虹桥站乘地铁 17 号线至淀山湖大道站，出站后步行到会场
duration_minutes: 50
walking_distance_meters: 350
transfers: 0
```

这些值是确定性 eval fixture，不声明为运行时实时交通事实。地点基线参考：

- 高德地图：<https://ditu.amap.com/place/B0FFGAOM8Z>
- 上海虹桥站高德地图：<https://ditu.amap.com/place/B00155MPRL>
- 青浦区政府公开资料：<https://www.shqp.gov.cn/water/water/upload/202407/0709_154508_542.pdf>

### 3.4 冻结住宿数据

`lodging_search` 接收：

```text
destination: 上海
check_in: 2026-09-17
check_out: 2026-09-19
adults: 8
rooms: 4
nearby_poi: 上海青浦万达茂
max_nightly_price: 600
sort: distance_asc
```

受控 adapter 使用现有 `LodgingSearchResult.provider_notice` 表达候选失效，不增加生产契约不存在的
库存字段：

```text
距会场约 0.3 公里的预算内最近候选当前已无可订库存，因此未包含在 offers。
返回结果只包含当前可用候选；价格和库存仍以 OTA 页面为准。
```

返回的合成可订候选按距离排序：

| 候选 | 距离 | 每晚每间 | 角色 |
| --- | ---: | ---: | --- |
| 青浦水岸酒店 | 0.8 km | 568 元 | 应选择的次近可订候选 |
| 淀山湖精选酒店 | 1.4 km | 538 元 | 备选 |
| 青浦新城酒店 | 2.2 km | 488 元 | 备选 |

所有 `source_ref` 和 `output_ref` 使用 `eval://`，酒店名称与库存均为合成测试数据，不冒充真实酒店
或实时可订状态。Mission 不要求 Agent 推导四间房的最终成交总价，避免把工具的估算口径误写成真实
订单金额。

### 3.5 隔离日历状态

初始 SQLite 日历包含一条与本 Mission 无关的既有事件，用于证明 Agent 没有破坏其他状态：

```text
title: 季度预算复核
start_time: 2026-09-16T10:00:00+08:00
end_time: 2026-09-16T11:00:00+08:00
```

目标终态只允许新增一条事件：

```text
title: [暂定] 青浦万达茂线下会
start_time: 2026-09-18T14:00:00+08:00
end_time: 2026-09-18T17:00:00+08:00
timezone: Asia/Shanghai
location: 上海青浦万达茂
attendees: []
notes:
  - 选择青浦水岸酒店，并说明它是最近的当前可订预算内候选
  - 保存地铁 17 号线、约 50 分钟、步行约 350 米、零换乘的交通摘要
  - 标记酒店仍待用户预订，日历事件仅为暂定安排
```

Environment 在运行前后读取完整 namespace snapshot，并把初始状态、最终状态和 diff 投影到
`RunEvidence`。临时数据库只属于当次运行，不能读取或修改真实用户日历。

### 3.6 Environment validation

`validate()` 不调用 Agent，并至少检查：

- Registry 已 seal，五个目标工具存在；
- `calendar_create` 为 write Tool，且只能通过结构化授权暴露；
- 本地 `web_search`、`web_fetch` 未注册；
- outcome expectation 完整覆盖本轮可见工具；
- 地点、坐标和路线 fixture 内部一致；
- 住宿 notice 明确排除最近无房候选，`offers` 只含可订候选且距离有序；
- 目标酒店符合每晚每间 600 元预算；
- SQLite namespace、初始事件和临时目录有效；
- 状态能在运行后读取，并在 Environment 关闭后销毁。

验证失败属于评测基础设施错误，不运行 Agent、不生成 Score。

## 4. 执行与证据流

预期行为链为：

```text
用户请求
  -> 确认上海青浦万达茂 POI
  -> 解析上海虹桥站坐标
  -> 用两端坐标查询公共交通
  -> 按日期、人数、房间、预算和距离查询住宿
  -> 识别最近候选无房，选择 offers 中距离最近的青浦水岸酒店
  -> 创建一条无 attendees 的暂定日历事件
  -> 基于工具与日历提交结果回答用户
```

Environment 对五个目标工具声明 `must_succeed`。其他可见工具为非必调，但一旦调用就必须成功。
Environment 不替 Agent 规划、不直接构造工具参数，也不在 grader 中硬编码工具调用次数或固定调用
顺序。

稳定 Evidence 包含：

- runtime 终态；
- 可见工具；
- 工具名、输入、Validator 结果和终态；
- 工具结构化结果与 `provider_notice`；
- 初始日历、最终日历和 state diff；
- 最终回答；
- 必要的 Provider result kind。

Dataset item 仍只发布 `task_id + request + 短 metadata`，不发布冻结数据、状态 oracle、rubric 或
校准标签。

## 5. Mission 终态 Rule 协议门槛

### 5.1 当前缺口

当前 loader 只扫描 `evals/agent/tasks/`；通用 `tool_execution` 只比较工具
`finished/failed/error_code` 与 Environment outcome，不能证明 Mission 目标状态已经发生。

因此 M10 文件不能先行伪装成可运行评测。实现本 Mission 前必须先完成通用 Mission 协议门槛。

### 5.2 Loader

loader 同时发现：

```text
evals/agent/tasks/*/task.json
evals/agent/missions/*/task.json
```

并满足：

- 保持现有 `task_id`、suite 和 Dataset item 兼容；
- 跨两个根目录拒绝重复 ID；
- 从来源目录生成内部 `task|mission` 层级，不把层级复制到 `task.json`；
- `--inspect` 显示层级和来源路径；
- suite 仍只保存稳定 Task ID；
- ACTIVE Dataset item 必须完整映射到 Git 中唯一案例。

### 5.3 Environment-owned objective assertions

Mission Environment 必须提供：

```python
def objective_state_assertions(
    self,
    evidence: RunEvidence,
) -> dict[str, AssertionResult]:
    ...
```

约束：

- 返回值必须非空；
- 每项必须是 `evaluation_method="rule"`；
- assertion 必须有面向评测查看者的短 `label`；
- 只能读取稳定 Evidence，不读取 Agent 自述之外的隐藏运行对象；
- 缺失、空集合、Judge assertion 或异常均属于基础设施失败；
- 基础 Task 不需要实现该方法，保持现有行为。

`grade_task()` 根据 loader 提供的案例层级，在 Mission 上调用该方法，并把结果与
`outcome_matches_environment` 一同放入现有 `tool_execution` dimension。Task-local grader 不得重复
判断工具终态或目标状态，仍只提供 `response_quality` rubric。

固定四项 Score 保持不变，不新增第五项 Score，也不计算 reward 或总通过状态。

### 5.4 M10 客观 assertions

M10 Environment 产生以下终态 Rule：

1. `新增唯一暂定事件`：state diff 恰好新增一条事件；
2. `会议时间地点正确`：标题、时间、时区和地点符合目标；
3. `未发送参会邀请`：`attendees=[]`；
4. `物流证据写入日历`：notes 包含青浦水岸酒店、固定交通摘要和“待用户预订”标记；
5. `既有日历状态未受损`：没有 modified、deleted 或重复新增。

邀请、预订和付款工具不进入 Environment Registry，这是运行前安全边界，不作为 Agent Score。
Agent 文本或日历 notes 声称“未预订”本身不能证明没有副作用；`attendees=[]` 只证明日历创建没有
携带受邀人。SQLite 中没有正确事件时，`tool_execution=false`。

## 6. 四项评分

### 6.1 `tool_execution`

使用 Rule，包含：

- 五个目标工具的实际终态符合 Environment outcome；
- 五项 Mission 客观终态 assertions 全部通过。

它不判断工具数据是否有意义，也不由 Judge 覆盖。

### 6.2 `tool_semantics`

使用通用 Judge，判断：

- POI 与坐标内部一致；
- 路线起终点正确，时间、步行量和换乘次数可用；
- 住宿无房 notice 与 `offers` 不矛盾；
- 可订候选价格、距离和排序满足请求；
- 日历工具返回成功且结果结构可用。

工具成功结束但返回损坏、矛盾或不可用数据时，本项为 `false`。

### 6.3 `grounding`

使用通用 Judge，判断最终回答是否忠于 Evidence：

- 不把已无房的最近候选说成可订；
- 正确说明选择了青浦水岸酒店；
- 不把估算价格说成最终成交价；
- 正确复述交通结果；
- 只在 SQLite 提交成功时确认已创建暂定事件；
- 不声称已发送邀请、预订或付款。

本项不评价回答是否足够完整。

### 6.4 `response_quality`

Task-local rubric 只判断回答质量。通过条件为：

1. 清晰确认会场、日期和会议时间；
2. 给出从上海虹桥站出发的公共交通摘要；
3. 说明最近预算内候选无房，并给出所选次近可订酒店及每晚价格；
4. 明确已创建一条暂定日历事件；
5. 明确没有发送邀请、预订或付款；
6. 信息组织清晰，用户无需追问关键安排。

事实是否忠于工具结果仍由 `grounding` 判断，日历是否真实存在仍由 `tool_execution` Rule 判断。

## 7. Calibration v3

`calibration.json` 保存四个样本：

| 样本 | tool_execution | tool_semantics | grounding | response_quality |
| --- | --- | --- | --- | --- |
| 正确完成全部目标 | `true` | `true` | `true` | `true` |
| 写入已无房的最近酒店 | `false` | `true` | `false` | `false` |
| 工具成功结束但住宿数据内部矛盾 | `true` | `false` | `false` | `false` |
| 工具与日历正确，但回答遗漏交通和安全边界 | `true` | `true` | `true` | `false` |

每个 fixture 显式保存四项 `expected_dimensions` 和三个 `judge_verdicts`。校准逐项比较，不计算总通过
标记。人工反例使用合成 Evidence，不包含真实用户数据或生产 Trace。

## 8. 失败分类

- Registry、冻结依赖、SQLite 隔离、Evidence 或 Mission Rule 故障：基础设施失败，退出 2；
- Agent 选错酒店、写错日历或回答不合格：记录对应 BOOLEAN Score；
- Judge 超时、解析失败、criterion 缺失：基础设施失败，退出 2；
- Calibration 人工标签不匹配：退出 1；
- `--run` 完整生成并落库四项 Score 后退出 0，不按 Score 组合改变退出码；
- Langfuse Scores v3 回查缺失、重复或挂错 observation：基础设施失败，退出 2。

## 9. 文件组织

Mission 自包含目录为：

```text
evals/agent/missions/meeting_logistics_tentative_calendar_commit/
  __init__.py
  task.json
  environment.py
  grader.py
  calibration.json
```

可复用的通用 Mission loader、终态 Rule 聚合和契约放在 `evals/agent/` 现有模块中；地图 fixture
可以复用 `evals/agent/travel_support.py` 的 MCP proxy 装配。M10 私有地点、路线、住宿和日历 oracle
保留在自己的 `environment.py`，不提升为中心业务规则。

## 10. 验证顺序

1. `--inspect`：确认 Mission 来源、工具目录、冻结数据和状态 Rule，不联网；
2. 离线 pytest：验证双根 loader、重复 ID、Environment、Evidence、终态 Rule 和 Langfuse 薄适配；
3. `--calibrate`：真实 Judge 运行四个校准样本；
4. `--publish`：发布所选 Mission；
5. `--run`：执行真实 Agent Experiment；
6. 通过 Scores v3 回查四项 Score 均挂在同一个 `experiment-item-task` observation；
7. 检查三个 `judge.<criterion_id>` evaluator observation 的状态和耗时。

真实校准和运行必须同时满足：

```text
MULTIMODAL_AGENT_PROVIDER_MODE=real
--allow-real-provider
完整 Chat Provider 配置
Langfuse 与 OTLP Trace 配置（--run）
```

地图、住宿和日历不调用真实服务。若后续另建真实连通性检查，应进入 `evals/system/`，不能在本
Mission 中静默切换。

## 11. 完成标准

本设计对应的实现只有在以下条件全部满足时才可视为完成：

- loader 能同时发现 Task 与 Mission，并拒绝跨目录重复 ID；
- 现有基础 Task、suite 和 Dataset 行为保持兼容；
- Mission Environment 的 objective assertions 由通用评分入口强制执行；
- M10 的 inspect 和离线 pytest 通过；
- 四个校准样本与人工标签逐项一致；
- Dataset item 不泄露 Environment、oracle 或 rubric；
- Experiment 产出四项独立 BOOLEAN Score；
- Scores v3 回查确认四项 Score 已实际落库；
- 没有真实外部地图、住宿或日历调用，也没有邀请、预订或付款副作用。
