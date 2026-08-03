# 购物结果 Runtime Detail 投影实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `shopping_search` 限制为每个 run 最多执行一次，精简其模型 observation，并由 Realtime 交付层把唯一成功结果确定性附加为 `<detail>` 协议。

**Architecture:** Assistant Runtime 在治理链执行前按结构化 Tool 名称和当前 run 的调用记录实施单次调用上限；购物 Tool 的完整 `ToolResult.data` 保留权威商品事实，`model_observation` 只保留 LLM 回答所需的精简商品摘要。Gateway Runtime Adapter 在终态读取唯一成功购物结果，保持自然语言正文不变，并仅为声明 `supports_shopping_detail_v1` 的入口附加确定性 detail 块。

**Tech Stack:** Python 3.11、Pydantic、LangGraph assistant loop、pytest、FastAPI WebSocket。

## Global Constraints

- 不修改 `ShoppingSearchTool.description`。
- `shopping_search` 每个 run 最多进入一次实际执行；Provider adapter 内部重试不计为第二次 Tool 调用。
- 第二次调用在 Executor 前拒绝，产生结构化 observation，但不自动终止其他 Tool 或整个任务。
- Presenter 只读取完整 `ToolResult.data`，不信任 LLM 文本或压缩 observation 中的 URL。
- 自然语言 `AgentResponse.message` 和 conversation history 不含 `<detail>`；只有支持能力的交付文本附加协议。
- 保留工作区内现有未提交改动，不覆盖生成图片展示等并行功能。
- Core invariant 不变；RED/GREEN 只写入 `tests/tdd/shopping-detail-runtime-projection/`。

---

### Task 1: 精简购物模型 Observation

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/tool.py`
- Test: `tests/tdd/shopping-detail-runtime-projection/test_shopping_observation.py`

**Interfaces:**
- Consumes: `ShoppingSearchResult.selections`
- Produces: `_model_observation(data) -> {"outcome", "summary", "total_cost", "within_budget", "uncovered_required_needs", "items"}`

- [ ] **Step 1: 写失败测试**

构造包含 candidates、selections、URL 和图片的 `ShoppingSearchResult`，执行真实 `_model_observation` 消费路径，断言 Provider-facing observation 只有：

```python
{
    "outcome": "success",
    "summary": "已选出 1 项商品候选，合计 2599.00 元。",
    "total_cost": 2599.0,
    "within_budget": True,
    "items": [{
        "product_id": "p1",
        "need": "小米14",
        "title": "小米14 12+256GB",
        "platform": "jd",
        "shop": "京东",
        "quantity": 1,
        "total_price": 2599.0,
        "currency": "CNY",
        "url_status": "unverified",
        "availability": "unknown",
    }],
}
```

并断言没有 `needs`、`selections`、`response_contract`、`rules`、`product_url` 或 `image_url`。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/shopping-detail-runtime-projection/test_shopping_observation.py
```

Expected: FAIL，因为当前 observation 仍包含重复层级、模板、URL 和图片。

- [ ] **Step 3: 最小实现**

把 `_model_observation` 收敛为公共摘要字段和最多三个 `_selection_observation`；每项只保留 need、标题、平台、店铺、数量、总价、币种、URL 状态和库存状态。

- [ ] **Step 4: 运行测试并确认 GREEN**

重复 Step 2，Expected: PASS。

### Task 2: Runtime 每 Run 单次购物调用

**Files:**
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Test: `tests/tdd/shopping-detail-runtime-projection/test_shopping_call_limit.py`

**Interfaces:**
- Consumes: `AgentState.tool_calls`
- Produces: 第二次 `shopping_search` 的 `ToolObservation.error.code == "run_tool_call_limit_reached"`

- [ ] **Step 1: 写失败测试**

通过真实 assistant decision guard/execution node fixture 建立已有 `shopping_search` ToolCallRecord，再请求不同参数的第二次调用，断言：

```python
assert observation["status"] == "rejected"
assert observation["error"]["code"] == "run_tool_call_limit_reached"
assert len(state.tool_calls) == 1
assert state.status == "running"
```

同时验证其他 Tool 不受该上限影响。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/shopping-detail-runtime-projection/test_shopping_call_limit.py
```

Expected: FAIL，因为不同参数的第二次购物调用目前仍会进入 Executor。

- [ ] **Step 3: 最小实现**

在 Runtime decision guard 中仅按 `SHOPPING_SEARCH_TOOL_NAME` 和 `state.tool_calls` 判断本 run 是否已有实际调用；给第二次 decision 增加 `run_tool_call_limit_reached` safety note。Executor 将它转换为 rejected observation，不进入 `ActionValidator -> ToolExecutor`，也不调用 `_enter_finalize_phase`。

- [ ] **Step 4: 运行测试并确认 GREEN**

重复 Step 2，Expected: PASS。

### Task 3: 确定性 Detail Presenter 与 Realtime 交付

**Files:**
- Create: `src/assistant_agent/gateway/shopping_detail.py`
- Modify: `src/assistant_agent/gateway/runtime_adapter.py`
- Test: `tests/tdd/shopping-detail-runtime-projection/test_shopping_detail_delivery.py`

**Interfaces:**
- Produces: `shopping_detail_block(tool_results: Sequence[ToolResult]) -> str`
- Produces: `shopping_detail_enabled(metadata: Mapping[str, Any]) -> bool`
- Consumes: `ShoppingSearchResult.model_validate(result.data)`

- [ ] **Step 1: 写 Presenter 失败测试**

断言最后一个成功购物结果的 `selections` 按原顺序输出最多三项：

```text
<detail>
1. 京东 - 小米14 12+256GB 2599元 <link>https://u.jd.com/one</link><pic>https://img.example/one.jpg</pic>
</detail>
```

并覆盖控制字符/协议标签清洗、非 HTTP(S) URL 跳过、无合格项返回空字符串。

- [ ] **Step 2: 写 Realtime 交付失败测试**

用真实 `GatewayRuntimeAdapter` 的本地 scripted run artifacts 断言：

- capability=true 时 `RealtimeAgentResult.response_text == natural + "\n" + detail`；
- `state.response.message` 仍为 natural；
- capability=false 时只返回 natural；
- 已有自然语言 response delta 时只补发 detail chunk，不重复自然语言；
- `response.delivered.source == "shopping_detail_v1"`。

- [ ] **Step 3: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/shopping-detail-runtime-projection/test_shopping_detail_delivery.py
```

Expected: FAIL，因为 presenter 和追加投影尚不存在。

- [ ] **Step 4: 最小实现**

在 `gateway/shopping_detail.py` 解析完整 Shopping result、验证价格/链接/图片并格式化 detail。`GatewayRuntimeAdapter` 仅在可信 entry capability 开启且 detail 非空时追加交付文本；如果自然语言 chunk 已发送，则只补发 `display_only=True, content_type="detail"` 的 detail chunk。

- [ ] **Step 5: 运行测试并确认 GREEN**

重复 Step 3，Expected: PASS。

### Task 4: Agent-Service 验收与权威文档同步

**Files:**
- Test: `tests/tdd/shopping-detail-runtime-projection/test_agent_service_shopping_detail.py`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/context_engineering_status.md`

**Interfaces:**
- Consumes: `RealtimeAgentResult.response_text`
- Produces: 最终 `intentResult.description` 中的自然语言加唯一 `<detail>`；中间/失败包不携带购物 detail

- [ ] **Step 1: 写失败验收测试**

构造 completed turn，断言 `_prepared_chat_response` 的最终 description 精确保留 Runtime Adapter 已投影的 detail；图片生成已有的 `intentResult.detail[]` 字段保持不变。

- [ ] **Step 2: 运行测试并确认 RED 或已有边界**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/shopping-detail-runtime-projection/test_agent_service_shopping_detail.py
```

若测试直接通过，说明 Agent-Service 已正确透传 Realtime result，本任务不为 wrapper 增加生产改动；保留测试作为本 feature 的显式验收。

- [ ] **Step 3: 同步文档**

明确记录：Runtime 每 run 一次 shopping 调用；observation 不携带展示模板/URL/图片；Realtime 交付层从完整 ToolResult 生成 detail；正文/history 不被覆盖；只有声明 capability 的入口接收 detail。

- [ ] **Step 4: 运行全部 feature 测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/shopping-detail-runtime-projection
```

Expected: PASS，且无网络访问。

- [ ] **Step 5: 静态检查**

```bash
git diff --check -- src/assistant_agent/runtime/assistant_loop_nodes.py src/assistant_agent/tools/plugins/builtin/shopping/tool.py src/assistant_agent/gateway/shopping_detail.py src/assistant_agent/gateway/runtime_adapter.py tests/tdd/shopping-detail-runtime-projection docs/tool-calling-architecture.md docs/runtime-event-stream-architecture.md docs/gateway-architecture.md docs/media-agent-service-websocket.md docs/context_engineering_status.md
```

Expected: 无输出、退出码 0。
