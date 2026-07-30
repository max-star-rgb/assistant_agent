# 中国大陆旅行助理能力实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 `assistant_agent` 中增加中国大陆旅行所需的高德地点/通勤能力，以及可返回飞猪酒店实时候选和 OTA 跳转链接的只读酒店搜索能力。

**Architecture:** 高德能力继续使用现有 MCP discovery、allowlist 和 `MCPToolAdapter`，不在 runtime 内重写地图 API；酒店能力扩展既有 `lodging` Plugin，通过同步、可注入 runner 的 `FlyAILodgingSearchAdapter` 调用官方 `flyai search-hotel` CLI，并归一化为稳定的 `LodgingSearchResult`。两类外部调用仍由本轮 Tool catalog 暴露并经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`，LLM 负责组合地点、路线和酒店证据生成行程，不新增关键词路由或多 Agent 编排。

**Tech Stack:** Python 3、Pydantic、现有 Tool Plugin/MCP 框架、FlyAI CLI、pytest。

## Global Constraints

- 默认 pytest 与运行保持 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得访问真实高德、飞猪或 LLM Provider。
- real mode 只有在 FlyAI Provider 与 CLI 路径显式配置完整时注册 `lodging_search`，不得回退到 mock。
- 只支持酒店搜索和 OTA 跳转，不执行预订、占房、付款或身份信息提交。
- 不安装 npm/Python 依赖，不写入或提交真实 API Key。
- 高德和 FlyAI 的结果都视为外部不可信数据，只把归一化后的最小字段提供给模型。
- 不读取请求文本进行 Tool 意图路由；候选工具空间只由 Plugin、MCP allowlist、category 和显式配置决定。

---

### Task 1: 扩展酒店稳定契约

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/models.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/tool.py`
- Test: `tests/contract/tools/test_lodging_tool.py`

**Interfaces:**
- Consumes: 现有 `LodgingSearchRequest`、`LodgingOffer`、`LodgingSearchResult`。
- Produces: 支持 `nearby_poi`、酒店类型、星级、床型、每晚预算和排序的请求；包含地址、坐标、评分、图片与 `booking_url` 的报价；模型 observation 最多暴露 3 个候选。

- [ ] **Step 1: 写失败的 Tool 契约测试**

验证：

```python
request = LodgingSearchRequest(
    destination="杭州",
    check_in=date(2026, 8, 1),
    check_out=date(2026, 8, 3),
    nearby_poi="西湖",
    star_ratings=[4, 5],
    max_nightly_price=800,
)
assert request.nearby_poi == "西湖"
assert request.star_ratings == [4, 5]
```

并验证 `LodgingSearchTool` 的 LLM schema 不包含内部 `limit`，成功 observation 中保留 `booking_url` 且最多 3 个 offers。

- [ ] **Step 2: 运行测试并确认因字段/隐藏参数缺失而失败**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/contract/tools/test_lodging_tool.py
```

Expected: FAIL，原因是新增请求字段或 `booking_url` 尚不存在。

- [ ] **Step 3: 实现最小稳定契约**

在 `LodgingSearchRequest` 增加：

```python
keywords: str | None
nearby_poi: str | None
hotel_types: list[Literal["酒店", "民宿", "客栈"]]
star_ratings: list[int]
bed_types: list[Literal["大床房", "双床房", "多床房"]]
max_nightly_price: float | None
sort: Literal["distance_asc", "rate_desc", "price_asc", "price_desc", "no_rank"]
limit: int = 5
```

在 `LodgingOffer` 增加可选的 `address`、`latitude`、`longitude`、`star`、`score`、`review`、`image_url`、`booking_url`，并把未知退订属性表示为 `refundable: bool | None`。`LodgingSearchTool.llm_hidden_input_fields = ("limit",)`，模型 observation 只投影前 3 个候选与 `observed_at`。

- [ ] **Step 4: 运行测试并确认通过**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/contract/tools/test_lodging_tool.py
```

Expected: PASS。

### Task 2: 实现 FlyAI 酒店 Adapter 与 fail-closed 装配

**Files:**
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/backend.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/__init__.py`
- Test: `tests/integration/tools/test_lodging_flyai.py`

**Interfaces:**
- Consumes: `LodgingSearchRequest` 与官方 `flyai search-hotel` 单行 JSON。
- Produces: `FlyAILodgingSearchAdapter.search(request) -> LodgingSearchResult`；`ProviderConfig.lodging_provider`、`flyai_cli_path`、`flyai_timeout_seconds`。

- [ ] **Step 1: 写失败的 Adapter 归一化测试**

使用可注入 fake runner 返回官方示例结构：

```json
{
  "status": 0,
  "message": "success",
  "systemMessage": "价格以飞猪页面为准",
  "data": {
    "itemList": [{
      "name": "杭州望湖宾馆",
      "price": "¥618",
      "detailUrl": "https://example.test/hotel/10021423",
      "mainPic": "https://example.test/hotel.jpg",
      "latitude": "30.259204",
      "longitude": "120.159246",
      "score": "5.0"
    }]
  }
}
```

断言命令参数包含目的地、日期、附近 POI 和预算；结果的 `nightly_price == 618`、`total_price == 1236`、`booking_url` 为详情链接。另测非零退出、timeout、非法 JSON、Provider `status != 0` 均返回结构化失败。

- [ ] **Step 2: 运行测试并确认 Adapter 尚不存在**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/integration/tools/test_lodging_flyai.py
```

Expected: FAIL，原因是 `FlyAILodgingSearchAdapter` 或配置字段尚不存在。

- [ ] **Step 3: 实现 CLI 调用与结果归一化**

使用不经过 shell 的参数数组：

```text
<flyai_cli_path> search-hotel
  --dest-name <destination>
  --check-in-date <YYYY-MM-DD>
  --check-out-date <YYYY-MM-DD>
  [--poi-name ...]
  [--key-words ...]
  [--hotel-types ...]
  [--hotel-stars 4,5]
  [--hotel-bed-types ...]
  [--max-price ...]
  [--sort ...]
```

runner 默认使用 `subprocess.run(..., shell=False, capture_output=True, text=True, timeout=...)`。只解析 stdout 的单个 JSON object；价格只接受可归一化的人民币数字；URL、坐标和评分交给 Pydantic 校验。外部错误使用稳定 code：`provider_unconfigured`、`provider_timeout`、`provider_bad_response`、`provider_unavailable`。

- [ ] **Step 4: 实现 real-mode 装配**

`ProviderConfig.from_env()` 读取：

```text
MULTIMODAL_AGENT_LODGING_PROVIDER=flyai
FLYAI_CLI_PATH=/absolute/path/to/flyai
FLYAI_TIMEOUT_SECONDS=30
```

mock mode 始终使用现有 mock adapter；real mode 只有 provider 为 `flyai` 且 CLI 路径非空时注册 `lodging_search`。未配置时不注册，禁止 mock fallback。

- [ ] **Step 5: 运行定向测试并确认通过**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/tools/test_lodging_flyai.py \
  tests/integration/runtime/test_hotel_price_watch.py
```

Expected: PASS。

### Task 3: 增加高德 MCP 的安全配置模板

**Files:**
- Modify: `deploy/mcp_servers.example.json`
- Modify: `.env.example`
- Test: `tests/contract/tools/test_mcp_config_examples.py`

**Interfaces:**
- Consumes: 官方 `@amap/amap-maps-mcp-server` 的 12 个只读 Tool。
- Produces: 可复制到 `.local/mcp_servers.json` 的 `amap_maps` server 配置；凭据只从父进程 `AMAP_MAPS_API_KEY` 注入。

- [ ] **Step 1: 写失败的配置模板契约测试**

解析 JSON 并断言 `amap_maps`：

```python
assert set(server["allowed_tools"]) == {
    "maps_regeocode",
    "maps_geo",
    "maps_ip_location",
    "maps_weather",
    "maps_search_detail",
    "maps_bicycling",
    "maps_direction_walking",
    "maps_direction_driving",
    "maps_direction_transit_integrated",
    "maps_distance",
    "maps_text_search",
    "maps_around_search",
}
assert server["read_only_tools"] == server["allowed_tools"]
assert server["enabled_tools"] == server["allowed_tools"]
assert server["env"]["AMAP_MAPS_API_KEY"] == "<set-in-ignored-local-config>"
```

- [ ] **Step 2: 运行测试并确认模板缺少高德配置**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/contract/tools/test_mcp_config_examples.py
```

Expected: FAIL，原因是找不到 `amap_maps`。

- [ ] **Step 3: 添加显式 stdio Server 配置**

命令使用本机 `npx`：

```json
[
  "/usr/bin/npx",
  "-y",
  "@amap/amap-maps-mcp-server@0.0.8"
]
```

12 个 Tool 全部列入 allowlist/read-only/default-enabled。模板只包含
`<set-in-ignored-local-config>` 占位符，用户复制到已忽略的 `.local/mcp_servers.json`
后再替换真实 Key；`.env.example` 说明首次运行会由 npx 下载上游包，且 Key 不得写回模板。

- [ ] **Step 4: 运行配置与 MCP 定向测试**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/contract/tools/test_mcp_config_examples.py \
  tests/integration/tools/test_mcp_sdk_environment.py
```

Expected: PASS。

### Task 4: 同步权威文档与完成验证

**Files:**
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/gateway-architecture.md`

**Interfaces:**
- Consumes: Tasks 1-3 的最终行为。
- Produces: 当前真实架构、配置方法、数据限制和人工真实联调步骤。

- [ ] **Step 1: 更新工具架构文档**

记录：

- 高德 12 个只读 MCP Tool 通过显式 allowlist 注册，LLM 自主组合 POI 与通勤工具。
- FlyAI 只由稳定 `lodging_search` Tool 包装，不引入其 Skill 的正则意图路由。
- 酒店价格、库存和跳转链接带 `observed_at`，最终成交条件以 OTA 页面为准。
- `lodging_search` 不预订、不支付；FlyAI CLI 未配置时 real registry 不注册该 Tool。

- [ ] **Step 2: 更新 Gateway 能力说明**

说明 API、CLI、WebSocket 均复用同一个 runtime/tool 治理链路，入口不做旅行关键词路由。

- [ ] **Step 3: 运行最小充分验证**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/contract/tools/test_lodging_tool.py \
  tests/contract/tools/test_mcp_config_examples.py \
  tests/integration/tools/test_lodging_flyai.py \
  tests/integration/tools/test_mcp_sdk_environment.py \
  tests/integration/runtime/test_hotel_price_watch.py
```

Expected: PASS。由于变更集中于 lodging/MCP 故障域且所有测试离线，不机械运行全量 pytest。

- [ ] **Step 4: 完成真实联调前置审计**

只做只读检查并报告，不安装或调用真实 Provider：

```bash
command -v npx
command -v flyai
test -n "${AMAP_MAPS_API_KEY:-}"
```

若 FlyAI CLI 或高德 Key 缺失，明确列为真实联调限制；不得把 fake 测试描述为真实服务验证。
