# 旅行信息规划、限定联网与应用跳转设计

日期：2026-08-19  
状态：已确认，待实施计划

## 1. 背景

项目已经具备以下旅行相关能力：

- `lodging_search`：查询指定日期的酒店候选、报价口径、库存状态和 Provider 返回的
  `booking_url`；当前真实实现由既有 FlyAI adapter 提供；
- 高德 MCP：地点检索、周边检索、地理编码以及步行、骑行、驾车和公共交通路线；
- Qwen Provider-native 联网：模型生成阶段可查询网页并返回搜索来源，不属于本地 Tool；
- `travel-tool-orchestration`：通过渐进式 Skill 加载治理酒店和高德旅行 Tool。

产品希望在个人开发者可承受的接入边界内，进一步支持国内旅行中的高铁、航班、景点、酒店、路线和攻略决策。
Agent 只提供信息、比较、建议和可点击跳转，不代替用户购买、预约或付款，也不承诺余票、库存、票价和运营状态。

当前 planning 实现存在两个与目标不一致的结构：

1. Planner 是一次裸的 `model.with_structured_output()` 调用，无法通过既有渐进式机制读取
   `travel-tool-orchestration`；
2. 每个 Worker 都重新调用同一个 `fast_agent`，但 Planner 已加载的 Skill 状态不能传给 Worker，容易造成每个
   Worker 重复执行 `load_skill`。

本设计同时解决旅行产品能力与 planning 上下文继承问题。

## 2. 目标

本次设计目标是：

1. Planner、Worker 和 Finalizer 都复用生产环境中同一个已编译 `AssistantFastAgent`，通过受信运行阶段调整
   可见 Tool、提示词、结构化输出和 Provider-native 搜索配置；
2. Planner 在发布旅行计划前，通过现有 `load_skill` 渐进加载 `travel-tool-orchestration`；
3. Planner 加载得到的 `active_skill_ids` 和 `skill_reference_grants` 保留在 planning 子图状态，并通过
   LangGraph 共享 state channel 与 `Send` 投影给 Worker；Worker 不重复加载同一个 Skill；
4. Planner 为每个计划节点选择一个预枚举 `search_profile`，LLM 不得提交任意域名或搜索参数；
5. Worker 根据节点 profile 使用百炼 Provider-native 联网，或根据已加载 Skill 调用酒店与高德 Tool；
6. 搜索来源、酒店预订链接及应用跳转在 Worker 边界结构化保留，Finalizer 不丢失或编造链接；
7. 所有旅行输出明确区分“已核验信息”“攻略性建议”和“待用户在目标渠道确认的信息”。

## 3. 非目标

本次不实现：

- 携程、12306、美团、小红书或航空公司的非公开接口、页面抓取、登录态复用和逆向协议；
- `travel_guide_search`、`travel_app_handoff`、`select_search_profile` 等新的本地 Tool；
- 购票、订房、预约、付款、锁价、占座、候补、退改签或订单管理；
- 将既有酒店 Provider 强行归一到携程，或把携程作为新的酒店事实源；
- 使用 Amadeus 作为中国国内航班的默认事实源；
- 修改高德 MCP 的连接、Tool schema、返回结构或路线语义；
- 新增美团餐饮推荐能力；餐饮若进入后续范围，应单独评估官方接入资格与跳转契约；
- 由 Root Graph 根据用户自然语言自动选择 `fast|planning`；入口仍必须显式传入
  `execution_mode="planning"`。

## 4. 外部能力约束

### 4.1 百炼限定联网

百炼官方文档允许在 `search_strategy="turbo"` 时使用 `assigned_site_list` 严格限定来源站点，最多配置
25 个站点；也允许通过 `intention_options.prompt_intervene` 用自然语言限定主题或来源范围。开启
`enable_source` 后，DashScope Generation API 可以返回搜索来源。

本设计仅以仓库已经采用的 DashScope 原生协议实现旅行 profile。若所选模型、地域或协议不支持某项搜索参数，
必须返回可解释的 capability error，不得静默改成不受限搜索。OpenAI-compatible 协议不能提供本设计需要的完整
来源交付时，不视为等价实现。

官方文档同时说明：指定站点没有相关内容时，模型仍可能使用自身知识回答；联网搜索达到限流时也可能不触发且不
报错。因此“已启用 profile”不等于“事实已经核验”。Worker 必须以实际返回的来源为依据确定核验状态；来源为空
时，不得把模型记忆写成实时事实。

### 4.2 国内交通与攻略

本期不依赖面向个人开发者的实时票务库存 API，也不调用网站内部接口：

- 高铁：检索 12306 官方公开网页、公告和可索引信息，提供候选车次或时刻，并引导用户到 12306 再确认；
- 航班：检索民航主管部门、机场和航空公司公开页面；在来源覆盖不足时，只给候选信息并明确待核验；
- 景点：开放时间、预约、票务、临时关闭等运营事实优先使用主管部门、景区运营方或其官方渠道；
- 小红书：只使用公开可索引页面和搜索跳转，不读取登录后内容，不调用内部接口；UGC 只支持体验、玩法、拥挤和
  避坑建议，不作为运营事实；
- 通用攻略：仅作为经验性建议；与官方事实冲突时，以适用日期明确的官方来源为准。

## 5. 总体拓扑

```text
AssistantRootGraph
  -> execution_router(execution_mode="planning")
  -> AssistantPlanningGraph
       -> PlannerSubgraph
            -> prepare private planner messages
            -> AssistantFastAgent(phase="planner")
                 -> load_skill("travel-tool-orchestration")
                 -> structured_response: NativePlanProposal
            -> project plan + activated skill state
       -> admission
       -> Send(WorkerState) × N
            -> WorkerSubgraph
                 -> AssistantFastAgent(phase="worker")
                 -> collect WorkerResult
       -> join
       -> FinalizerSubgraph
            -> AssistantFastAgent(phase="finalizer")
       -> standard AIMessage
```

`AssistantFastAgent` 只构造和编译一次。Planner、Worker、Finalizer 是同一 compiled graph 的不同调用阶段，
不是三套 Agent Runtime。外围显式 StateGraph 只负责数据准备、DAG admission、`Send`、结果汇总和阶段切换。

Planner/Worker 的私有 `messages` 不直接并入父图公开会话，避免把 `load_skill`、结构化计划提交和 Worker 中间
ToolMessage 暴露为最终聊天历史。父子图通过显式共享 channel 传递能力状态，通过窄输入/输出 schema 投影计划与
结果。

### 5.1 官方模式依据与组合边界

LangChain/LangGraph 当前没有一份官方示例同时覆盖“`create_agent` Planner 先加载 Skill、输出 DAG、再把同一
Skill 的不同 Tool 子集和不同 Provider-native 搜索配置并行派发给 `create_agent` Worker”。本设计不把这一组合
误称为框架内置模板，而是严格组合以下已有官方模式：

1. **Custom workflow**：官方允许在显式 `StateGraph` 的任意节点中调用完整 `create_agent`，以混合确定性步骤与
   agentic behavior；
2. **Router + Send**：官方 multi-source router 示例先产生结构化分类，再由普通路由函数把每项分类转换为
   `Send(target, narrow_state)`，每个 Agent 只收到自己的 query；
3. **Subgraph state**：`create_agent` 本身是 compiled LangGraph；共享 schema 时可以直接作为子图，schema 不同
   时使用窄 adapter 节点做输入/输出转换；默认 per-invocation 子图状态支持并行且继承父图 checkpointer；
4. **Dynamic Tool middleware**：官方 `wrap_model_call` 支持按 state/context 过滤预注册 Tool；
5. **Dynamic response format**：官方 middleware 可以按阶段覆盖 `ModelRequest.response_format`，使 Planner 在
   Tool loop 结束时返回结构化计划。

因此 Planner 负责**声明**任务目标、依赖和所需能力，真正调用 `Send` 的是确定性 dispatcher。模型不创建
`Send`、不提交任意 state payload，也不决定 checkpoint namespace。

官方 supervisor/subagent 模式更适合由主 Agent 通过 Tool 动态委派对话任务；本项目需要本地 DAG admission、
按依赖分 wave、显式并行 state 和统一 Finalizer，因此采用官方 custom workflow/router 组合，而不引入
`langgraph-supervisor`。Deep Agents 的自定义 subagent Skill state 默认与父 Agent 隔离，也不满足“Planner
加载一次、Worker 继承受信 Skill”的目标。

## 6. Agent 阶段契约

新增受信运行阶段，不从用户文本推断：

```python
AgentPhase = Literal["fast", "planner", "worker", "finalizer"]
```

阶段由 Graph 拓扑写入 invocation context 或受信 state：

| phase | 可见能力 | Provider-native 搜索 | 输出 |
| --- | --- | --- | --- |
| `fast` | 保持现有渐进式 Tool exposure | 保持既有 fast 配置 | 普通 `AIMessage` |
| `planner` | Skill index、`load_skill`；不暴露酒店、高德等业务 Tool | 强制关闭 | `NativePlanProposal` |
| `worker` | 仅暴露节点继承 Skill 与 `allowed_tool_names` 的交集 | 由节点 `search_profile` 决定 | `WorkerResult` |
| `finalizer` | 不暴露业务 Tool 和 `load_skill` | 强制关闭 | 最终 `AIMessage` |

阶段控制由 `create_agent` middleware 基于受信 context/state 完成，可动态调整 system prompt、
`ModelRequest.tools`、`ModelRequest.response_format` 和模型请求参数。Planner 的结构化响应使用 LangChain
`response_format`：Provider 支持时可使用 `ProviderStrategy`，否则使用 `ToolStrategy`。后者形式上可能表现为
结构化提交 Tool call，但不执行外部业务逻辑。

## 7. Planner 与 Skill 激活

Planner 第一次模型调用只获得精简 Skill index。旅行请求匹配 index 中的
`travel-tool-orchestration` 后，应先调用：

```text
load_skill("travel-tool-orchestration")
```

成功后：

1. `load_skill` 按现有受信 manifest 重新解析 Skill，不接受模型返回的任意 Tool grant；
2. `active_skill_ids` 与 `skill_reference_grants` 写入 Planner/Planning 共享 state channel；
3. 下一次 Planner model call 得到旅行 Skill 正文和 `NativePlanProposal` response schema；
4. `phase="planner"` 的 Tool filter 仍不暴露 `lodging_search` 和高德业务 Tool；
5. Planner 返回计划，而不执行查询。

`travel-tool-orchestration` 仍属于渐进式加载：只是加载发生在 planning 的 Planner 阶段，而不是各 Worker
自行重复发生。

## 8. 计划与状态模型

### 8.1 搜索 Profile 枚举

```python
ProviderSearchProfile = Literal[
    "none",
    "rail_official",
    "flight_official",
    "guide_official",
    "guide_xiaohongshu",
    "travel_general",
]
```

LLM 只能选择枚举值。域名列表、搜索策略、是否强制搜索、检索范围提示和来源要求全部来自本地受信 policy registry，
不得出现在 Planner 输出 schema 中。

### 8.2 Plan node

```python
class NativePlanNode(BaseModel):
    node_id: str
    objective: str
    depends_on: tuple[str, ...] = ()
    required_skill_ids: tuple[str, ...] = ()
    allowed_tool_names: tuple[str, ...] = ()
    search_profile: ProviderSearchProfile = "none"
```

`required_skill_ids` 只允许引用 Planner 本轮已经成功激活的 Skill。Planner 不得通过该字段加载新 Skill。
`allowed_tool_names` 只能从这些 Skill 当前 manifest 的 `governed_tools` 中选择。Planner 加载 Skill 后获得由
受信 catalog 生成的规划能力索引（Tool 名、用途和 effect，不含凭据与执行实现），用于为各节点选择最小 Tool
子集；该索引不等于向 Planner 暴露可调用的业务 Tool schema。

### 8.3 Planning 与 Worker state

```python
class PlanningState(AgentState):
    plan: NativePlanProposal
    active_skill_ids: list[str]
    skill_reference_grants: dict[str, list[str]]
    worker_results: Annotated[list[WorkerResult], operator.add]

class WorkerState(FastAgentState):
    work_item_id: str
    objective: str
    dependency_results: tuple[WorkerResult, ...]
    worker_tool_allowlist: tuple[str, ...]
    search_profile: ProviderSearchProfile
```

Planning 专用 Skill channel 不进入 Root Graph 的长期 Memory，也不改变 fast 分支的公开输入。`Send` 对每个节点只
投影 `required_skill_ids` 指定的 Skill 子集，并把 admission 后的 `allowed_tool_names` 写入
`worker_tool_allowlist`。Worker 的最终 Tool 可见集合为：

```text
本轮静态 eligible Tool inventory
  ∩ active_skill_ids 对应的 governed_tools
  ∩ worker_tool_allowlist
  ∩ 既有 media/env/permission 条件
```

因此搜索节点可以继承旅行 Skill 的规则但看到零个本地旅行 Tool，酒店节点只看到 `lodging_search`，路线节点只看到
所需高德 Tool。不同 Worker 不会因为其他并行节点需要某项能力而看到无关 Tool。该字段在 Worker 输入中必须存在：
空列表明确表示禁止全部本地 Tool，不能与“字段缺失，沿用 fast 默认 exposure”混同。

### 8.4 确定性 Worker state 投影

Planner 不直接构造或发送 `WorkerState`。admission 完成后，dispatcher 使用受信父状态和 plan node 生成
`Send`：

```python
def dispatch_ready(state: PlanningState) -> list[Send]:
    sends = []
    for node in ready_nodes(state):
        skill_ids = admit_skill_subset(
            requested=node.required_skill_ids,
            activated=state["active_skill_ids"],
        )
        tool_names = admit_tool_subset(
            requested=node.allowed_tool_names,
            governed_by=skill_ids,
            inventory=trusted_tool_inventory,
        )
        sends.append(
            Send(
                "worker",
                {
                    "messages": [HumanMessage(content=node.objective)],
                    "work_item_id": node.node_id,
                    "dependency_results": direct_dependencies(node, state),
                    "active_skill_ids": list(skill_ids),
                    "skill_reference_grants": project_reference_grants(
                        skill_ids, state
                    ),
                    "worker_tool_allowlist": list(tool_names),
                    "search_profile": node.search_profile,
                    "agent_phase": "worker",
                },
            )
        )
    return sends
```

每个 `Send` 获得独立的 per-invocation state；列表、字典和消息按值构造，不在并行 Worker 间共享可变对象。
Worker 只能读取自己的 state，输出通过 `worker_results` reducer 回到父图。

### 8.5 Admission

除既有 ID、依赖引用和 DAG 无环校验外，admission 必须校验：

- `required_skill_ids` 无重复且全部属于 Planner 已激活集合；
- `allowed_tool_names` 无重复，且全部属于 `required_skill_ids` 对应 Skill 的当前受信 `governed_tools`；
- `search_profile` 是受支持枚举；
- 非 `none` 的旅行 profile 必须由 `travel-tool-orchestration` 治理；
- profile 对当前 Provider/model/protocol 不可用时，在派发前失败并返回可解释错误；
- Planner 未加载旅行 Skill 却提交旅行 profile 时拒绝计划，而不是让 Worker 临时补加载。

## 9. Profile policy registry

初始策略如下：

| profile | 搜索 | 主要范围 | 使用场景 |
| --- | --- | --- | --- |
| `none` | 显式关闭 | 无 | 酒店 Tool、高德 Tool、纯推理或仅消费依赖结果 |
| `rail_official` | `turbo`、强制搜索、返回来源 | `assigned_site_list=["12306.cn"]` | 官方车次、时刻、公告和规则 |
| `flight_official` | `turbo`、强制搜索、返回来源 | 本地受信民航、机场、航空公司域名表，最多 25 项 | 航班候选、机场和航司公告 |
| `guide_official` | `turbo`、强制搜索、返回来源 | 受信景区/文旅官方域名表；不足时增加“仅官方运营主体”范围提示 | 开放、预约、票务、临时公告 |
| `guide_xiaohongshu` | `turbo`、强制搜索、返回来源 | `assigned_site_list=["xiaohongshu.com"]` | 公开可索引的游记、体验和避坑 |
| `travel_general` | `turbo`、强制搜索、返回来源 | 不设任意 LLM 域名；使用本地固定的旅行主题范围提示 | 官方 profile 覆盖不足的通用攻略兜底 |

`flight_official` 和 `guide_official` 的域名表属于代码或受信静态配置，实施时必须有 schema 校验、去重和最多
25 项限制。LLM 不能添加、删除或覆盖域名。不能把仅有自然语言 `prompt_intervene` 的结果描述为“严格站点限定”。

planning 中 profile 是显式请求级覆盖：

- `planner`、`finalizer` 和 `profile="none"` 必须关闭联网，即使进程级
  `QWEN_CHAT_ENABLE_SEARCH=true`；
- Worker 的非 `none` profile 显式开启联网；
- fast 模式未携带 planning profile 时保持当前进程级配置兼容行为；
- 不根据 objective 关键词在 adapter 内二次猜测 profile。

## 10. Planner 拆分规则

旅行 Skill 指导 Planner 按事实类型拆分节点，而不是把所有查询放进一次不受控搜索：

- 高铁候选与官方公告：`rail_official`；
- 航班候选与官方公告：`flight_official`；
- 景点运营事实：`guide_official`；
- 小红书体验与攻略：`guide_xiaohongshu`；
- 酒店查询与比较：`none`，由 `lodging_search` 执行；
- 地点与路线：`none`，由既有高德 MCP 执行；
- 只有确实无法归入窄 profile 的经验性内容才使用 `travel_general`。

同一目的地同时需要“官方开放信息”和“小红书体验”时，应拆成两个可并行节点。需要根据已选车次、航班或酒店
继续规划路线时，路线节点通过 `depends_on` 读取直接上游结果。

示例：

```json
{
  "schema_version": "native_plan_v1",
  "nodes": [
    {
      "node_id": "rail",
      "objective": "查询指定日期北京到上海的官方高铁候选和时刻",
      "depends_on": [],
      "required_skill_ids": ["travel planning"],
      "allowed_tool_names": [],
      "search_profile": "rail_official"
    },
    {
      "node_id": "hotel",
      "objective": "按用户日期和预算比较上海酒店候选",
      "depends_on": [],
      "required_skill_ids": ["travel planning"],
      "allowed_tool_names": ["lodging_search"],
      "search_profile": "none"
    },
    {
      "node_id": "official_guide",
      "objective": "核验候选景点开放、预约和临时公告",
      "depends_on": [],
      "required_skill_ids": ["travel planning"],
      "allowed_tool_names": [],
      "search_profile": "guide_official"
    },
    {
      "node_id": "xiaohongshu_guide",
      "objective": "补充公开可索引的小红书体验和避坑建议",
      "depends_on": [],
      "required_skill_ids": ["travel planning"],
      "allowed_tool_names": [],
      "search_profile": "guide_xiaohongshu"
    },
    {
      "node_id": "route",
      "objective": "根据车次、酒店和景点结果规划关键通勤路线",
      "depends_on": ["rail", "hotel", "official_guide"],
      "required_skill_ids": ["travel planning"],
      "allowed_tool_names": [
        "mcp_amap_maps_maps_geo",
        "mcp_amap_maps_maps_direction_transit_integrated"
      ],
      "search_profile": "none"
    }
  ]
}
```

## 11. 来源与链接交付

当前 `WorkerResult` 只保存文本，会丢失 `AIMessage.response_metadata.provider_search_sources`。本设计扩展为：

```python
class EvidenceLink(BaseModel):
    title: str
    url: str
    domain: str | None = None
    source_kind: Literal["provider_search", "tool", "official", "ugc"]

class AppHandoff(BaseModel):
    app_id: Literal["12306", "airline", "airport", "xiaohongshu", "ota", "amap", "web"]
    action: Literal["search", "detail", "verify", "book", "navigate"]
    label: str
    web_url: str
    app_uri: str | None = None
    provenance: Literal["provider_source", "tool_result", "trusted_template"]

class WorkerResult(BaseModel):
    work_item_id: str
    content: str
    verification_status: Literal["verified", "advisory", "unverified", "failed"]
    sources: tuple[EvidenceLink, ...] = ()
    handoffs: tuple[AppHandoff, ...] = ()
```

链接来源规则：

1. Provider-native 搜索来源从 terminal `AIMessage.response_metadata` 机械提取，不能让模型自行重建 URL；
2. 酒店跳转只使用 `lodging_search` 本轮 Tool artifact 返回的 `booking_url`；
3. 可信跳转模板只允许来自代码内 allowlist，并对 query 参数做 URL 编码；不使用未经官方确认的私有 URI scheme；
4. 小红书只提供公开搜索/详情页或经验证的应用 URI，不绕过登录或访问控制；
5. 高德 MCP 本期不改 schema；只有工具本身返回可验证 URL 或已有可信模板时才生成跳转；
6. `web_url` 是最低保证，`app_uri` 仅在有稳定、受信模板时出现；具体能否唤起原生 App 仍取决于客户端平台、
   Universal Link/App Link 配置和安装状态，客户端必须回退到 `web_url`；
7. 没有真实链接时明确“无可用跳转”，不得为了满足格式拼接猜测 URL。

`AppHandoff` 是内部结构化结果，不是模型可调用 Tool。Finalizer 将其渲染为可点击 Markdown 链接；支持富 UI 的
客户端可直接消费结构化字段。用户点击后只进入目标渠道查看，不代表 Agent 已完成购买或预约。

## 12. 各能力的事实与跳转边界

### 12.1 高铁

- 输出候选车次、出发/到达站、日期与时刻时，必须有本轮 `rail_official` 来源；
- 无来源或来源日期不匹配时，只能返回“未核验/请在 12306 查看”；
- 最终提供 12306 官方页面或可信应用跳转，不声称实时余票、票价或可购买性。

### 12.2 航班

- 优先采用航空公司、机场和民航主管部门公开来源；
- 不把计划时刻、动态状态、销售库存和票价混为一谈；
- 多来源冲突时保留冲突并建议进入相应航司/机场渠道核验；
- 国内覆盖不足时明确限制，不使用 Amadeus 或模型记忆补齐完整航班表。

### 12.3 景点与攻略

- 官方开放、预约、票务和临时公告由 `guide_official` 节点负责；
- 小红书和通用攻略节点只输出体验性建议，并附公开来源或搜索跳转；
- UGC 推荐时长、拥挤程度和避坑经验不得写成官方承诺；
- 官方事实与 UGC 冲突时分别呈现，Finalizer 不做无来源裁决。

### 12.4 酒店

- 继续使用 provider-neutral 的 `lodging_search` 业务模型；
- 不新增携程 adapter，不把 FlyAI 返回结果改称携程数据；
- 酒店候选、价格、库存和 `booking_url` 必须来自同一次 Tool result；
- OTA 链接仅供用户查看，价格和库存以用户打开目标渠道时为准。

### 12.5 路线

- 地点消歧、坐标和路线继续使用既有高德 MCP；
- 不修改高德 Tool schema，也不使用 Provider-native 搜索估算路线时间；
- 路线缺少未来出发时间语义时，只描述为当前规划参考；
- 路线节点可以消费交通、酒店和景点节点的结构化结果，但依赖结果始终作为不可信只读数据处理。

## 13. Finalizer 契约

`phase="finalizer"` 的 `AssistantFastAgent` 只接收：

- 用户原始请求；
- 已 admission 的计划摘要；
- 按计划顺序排列的 `WorkerResult`；
- 父图冻结的可信时间与地点事实。

Finalizer 不联网、不调用 Tool、不重新生成来源 URL。它必须：

- 综合行程而不是描述内部 Planner/Worker；
- 保留来源与候选的一一对应；
- 将 `verified`、`advisory`、`unverified`、`failed` 转成用户可理解的信息状态；
- 对价格、余票、库存、开放和时刻保留观察时间及适用日期；
- 输出可点击链接并提醒用户在目标渠道确认；
- 不声称已购买、预约、锁价或完成 App 内操作。

## 14. 失败与降级

- `load_skill` 失败：Planner 不得发布引用该 Skill/profile 的计划；返回可解释失败；
- Planner 结构化输出非法：由 Agent 结构化输出纠错和节点 retry 处理；admission 仍是最终本地边界；
- profile 不受当前模型或协议支持：节点在执行前失败，不降级到开放互联网搜索；
- 百炼搜索无来源：Worker 返回 `unverified`，不得使用模型记忆补写实时事实；
- 单个 Worker 失败：不阻塞无依赖节点，Finalizer 披露缺失项；
- 酒店 Provider 未配置或失败：不使用地图 POI 或联网搜索伪造酒店价格与库存；
- 高德 MCP 失败：保留可完成的信息查询，路线标为待确认；
- 跳转模板不可用：保留真实来源 URL；两者都没有时明确无可用链接。

所有 Provider-backed 能力继续遵循 `mock|real` 硬边界。mock 模式使用确定性 mock，不访问真实网络；真实联网
必须同时满足 `MULTIMODAL_AGENT_PROVIDER_MODE=real`、Qwen/DashScope 完整配置和 profile capability 校验。

## 15. 安全与治理

- 不把用户文本、搜索摘要或依赖节点内容当成可信 Tool/profile 配置；
- 不允许 LLM 提交域名、HTTP 参数、API key、URI 模板或 effect metadata；
- 搜索结果、Tool observation 和 dependency results 都按不可信外部内容处理，不能覆盖系统规则；
- URL 只允许 `https` 和经过 allowlist 的应用 scheme，拒绝凭据、`javascript:`、本地文件和未知 scheme；
- Provider 原始响应不写入 checkpoint、日志或最终 artifact，只保留清洗后的来源；
- Skill 激活不进入长期 Memory，不跨用户或跨 thread 泄漏；
- 本期全部能力为 read-only；未来新增写操作必须遵循 planning 模式原生 HITL。

## 16. 可观测性

沿用 LangGraph/LangSmith 原生 trace，并记录有界结构化字段：

- `agent_phase`；
- Planner 激活的 `active_skill_ids`；
- 每个节点的 `required_skill_ids` 与 `search_profile`；
- profile capability admission 结果；
- Worker 是否实际收到 Provider 来源及来源数量；
- `verification_status`；
- handoff 数量与 `provenance`，不记录用户凭据或完整 Provider 原始响应。

不得新增平行 run/event runtime 或把 Skill 正文、网页全文写入业务日志。

## 17. 验收标准

1. `execution_mode="planning"` 的旅行请求由 Planner 阶段的同一个 `AssistantFastAgent` 先调用一次
   `load_skill("travel-tool-orchestration")`，再返回结构化计划；
2. Planner 不执行 `lodging_search`、高德 MCP 或 Provider-native 搜索；
3. 多个旅行 Worker 继承 Planner 激活的 Skill，不重复调用 `load_skill`；
4. 不同 Worker 只看到各自 `worker_tool_allowlist` 与 Skill grants 的交集；搜索 Worker 不看到酒店/高德 Tool，
   酒店和路线 Worker 也不获得无关旅行 Tool；
5. Planner 只能从六个 profile 枚举中选择，Provider 请求中不出现 LLM 提交的任意域名；
6. 酒店与路线节点使用 `none`，交通/攻略 Worker 分别使用对应窄 profile；
7. `rail_official` 严格限定到 12306；`guide_xiaohongshu` 严格限定到小红书公开站点；
8. profile 限制不受支持时 fail closed，不静默执行开放搜索；
9. Worker 的搜索来源能够经过 `WorkerResult` 到达 Finalizer，最终链接不因 planning 汇总而丢失；
10. 酒店链接只来自 Tool，搜索链接只来自 Provider metadata 或受信模板；无来源时不编造 URL；
11. 用户可以点击 HTTPS 跳转查看，支持的客户端可优先尝试 `app_uri` 并回退 `web_url`；
12. 输出清楚区分官方事实、攻略建议和待确认项，不承诺实时余票、库存、票价或预订完成；
13. 高德 MCP 保持不变，项目不新增 `travel_guide_search`、`travel_app_handoff` 或搜索 profile Tool；
14. mock 测试不调用真实 Provider；real 验证必须通过显式开关并在最终报告说明调用范围。

## 18. 实施影响范围

预计实施会涉及：

- `src/assistant_agent/native_agent/models.py`：profile、plan node、来源与 handoff 结构；
- `src/assistant_agent/native_agent/state.py`：phase、planning Skill channel 和 Worker profile；
- `src/assistant_agent/native_agent/planning_graph.py`：Planner/Worker/Finalizer 复用 fast graph、子图状态投影；
- `src/assistant_agent/native_agent/fast_agent.py` 及 middleware：阶段化 Tool、prompt、response format 和搜索参数；
- `src/assistant_agent/native_agent/tool_exposure.py`：Planner 可加载但不可执行业务 Tool、Worker 继承 exposure；
- `src/assistant_agent/providers/dashscope_langchain.py` 与 Provider 装配：预枚举 policy、显式关闭、站点限定和来源；
- `skills/travel-tool-orchestration/SKILL.md` 与 `skill.toml`：规划拆分、profile 选择和链接边界；
- 对应 TDD/core 测试与 `docs/runtime-event-stream-architecture.md`、`docs/tool-calling-architecture.md`。

具体测试归属和是否晋升 core 在实施计划阶段按 `tests/README.md` 与项目测试 skill 决定。

## 19. 官方参考

- [LangGraph Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)：`create_agent` 可作为
  compiled subgraph，父子图共享同名 state channel；
- [LangGraph Graph API / Send](https://docs.langchain.com/oss/python/langgraph/use-graph-api)：并行 Worker
  的独立输入 state 与 map-reduce 派发；
- [LangChain Multi-agent Router](https://docs.langchain.com/oss/python/langchain/multi-agent/router-knowledge-base)：
  结构化分类、每个 `Send` 的窄 Agent 输入、并行 `create_agent` Worker 与 reducer 汇总；
- [LangChain Custom Workflow](https://docs.langchain.com/oss/python/langchain/multi-agent/custom-workflow)：
  在显式 `StateGraph` 节点内复用完整 `create_agent`；
- [LangChain Middleware](https://docs.langchain.com/oss/python/langchain/middleware/overview)：在更大
  StateGraph 中复用 Agent middleware；
- [LangChain Structured Output](https://docs.langchain.com/oss/python/langchain/structured-output)：
  `ProviderStrategy`、`ToolStrategy` 与 `structured_response`；
- [阿里云百炼联网搜索](https://help.aliyun.com/zh/model-studio/web-search)：`assigned_site_list`、
  `prompt_intervene`、来源返回、策略限制与无搜索结果时的行为。
