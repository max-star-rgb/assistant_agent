# 统一真实购物工具实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `shopping_search` 与 `shopping_list_search` 合并为唯一的 `shopping_search`，并保证运行中的 Agent 永远不会注册或返回 mock 购物结果。

**Architecture:** 对模型只暴露基于 `needs[]` 的统一请求；单品是一个 need，多品类是多个 need。工具内部继续复用现有单关键词 Provider adapter，对每个 need 执行搜索与比价，再按单价上限、数量和可选总预算组合候选。Shopping Plugin 在 mock mode 或真实 Provider 未完整配置时不注册任何购物工具。

**Tech Stack:** Python 3.12、Pydantic、现有 Tool Plugin/Registry、好单库与 HTTP adapter、pytest。

## Global Constraints

- 所有真实调用仍经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- mock mode 不注册购物 Tool；pytest 只能显式注入 deterministic fake adapter。
- real mode 只在 `haodanku` 或 `http` 搜索与比价配置完整时注册唯一 `shopping_search`。
- 不提供下单能力；商品、价格、图片和链接只能来自真实 Provider observation。
- 多于一个 need 时 `total_budget` 必填；一个 need 时可仅使用 `max_unit_price`。
- 不保留 `shopping_list_search` 兼容 Tool、Tool ID 或模型可见 schema。

---

### Task 1: 锁定唯一 Tool 与 real-only 注册边界

**Files:**
- Modify: `tests/integration/runtime/test_runtime_extended_behaviors.py`
- Modify: `tests/integration/tools/test_tool_plugin_l2.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/plugin.py`
- Modify: `src/assistant_agent/tools/ids.py`

**Interfaces:**
- Consumes: `ShoppingToolPlugin.build_tools(context: ToolPluginContext) -> list[Tool]`
- Produces: real 配置完整时仅含 `shopping_search`，其他模式为空列表。

- [ ] **Step 1: 写失败测试**

```python
assert "shopping_search" not in AgentGraphRuntime(config=_offline_config()).registry.list()
assert [tool.name for tool in ShoppingToolPlugin().build_tools(real_context)] == ["shopping_search"]
assert "shopping_list_search" not in registry.list()
```

- [ ] **Step 2: 运行测试验证失败**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/runtime/test_runtime_extended_behaviors.py \
  tests/integration/tools/test_tool_plugin_l2.py
```

Expected: FAIL，mock registry 仍包含 `shopping_search`，Plugin 仍返回两个 Tool。

- [ ] **Step 3: 最小修改注册逻辑**

`ShoppingToolPlugin.build_tools()` 在 `context.mock_mode` 时直接返回 `[]`；真实 Provider 不完整时返回 `[]`；完整时只构造 `ShoppingSearchTool`。删除 `SHOPPING_LIST_SEARCH_*` 标识符。

- [ ] **Step 4: 运行定向测试**

Expected: 注册边界相关断言通过。

### Task 2: 合并输入、执行和输出契约

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/models.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/tool.py`
- Delete: `src/assistant_agent/tools/plugins/builtin/shopping/list_tool.py`
- Modify: `tests/integration/shopping/test_shopping_list_search.py`
- Modify: `tests/integration/shopping/test_shopping_result_outcomes.py`
- Modify: `tests/integration/shopping/test_shopping_comparability.py`

**Interfaces:**
- Produces: `ShoppingSearchRequest(needs, total_budget?, scenario?, decision_reason?, evidence, platforms, top_k_per_need)`
- Produces: `ShoppingSearchResult(outcome, needs, selections, total_cost, within_budget, uncovered_required_needs, errors, provider, output_refs)`
- Consumes: 现有 `ProductSearchAdapter.search()` 与 `PriceCompareAdapter.compare()`。

- [ ] **Step 1: 更新测试为统一 `shopping_search`**

```python
tool = ShoppingSearchTool(search_adapter=search, compare_adapter=compare)
result = tool.run({"needs": [{"keyword": "通勤电脑双肩包", "max_unit_price": 500}]})
assert result.data["needs"][0]["status"] == "selected"
```

增加多 need 总预算、单 need 可选总预算、重复关键词拒绝、部分失败和全部失败用例。

- [ ] **Step 2: 运行 shopping 测试验证失败**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/integration/shopping
```

Expected: FAIL，旧 `ShoppingSearchRequest` 仍要求 `query`，清单仍由独立 Tool 执行。

- [ ] **Step 3: 实现统一模型与工具**

为 Provider adapter 保留私有单关键词请求模型；公开请求改为 `needs[]`。每个 need 依次：

```python
search = search_adapter.search(provider_request)
comparison = compare_adapter.compare(compare_request) if search.items else None
```

候选优先使用 comparison offers；没有可用 offer 时保留搜索候选并标记 partial。组合算法优先覆盖 required needs，再覆盖总项数，再按候选排名和总价选择。

- [ ] **Step 4: 删除独立清单 Tool**

删除 `list_tool.py`，将仍需的组合逻辑并入 `tool.py`，移除所有生产导入。

- [ ] **Step 5: 运行 shopping 测试**

Expected: 单品和清单均通过唯一 Tool，失败语义保持 `success|partial|empty|failed`。

### Task 3: 移除运行时 mock 路径并同步契约

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/backend.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `.env.example`
- Modify: `src/assistant_agent/runtime/capability_models.py`
- Modify: `docs/tool-calling-architecture.md`
- Modify: affected tests under `tests/integration/runtime/` and `tests/integration/shopping/`

**Interfaces:**
- Produces: Tool 构造必须显式获得 adapter，或由 real-only Plugin factory 注入。
- Produces: mock Provider 值只能作为全局禁用状态，不得创建 mock shopping adapter。

- [ ] **Step 1: 写失败测试**

```python
with pytest.raises(ValueError):
    create_shopping_search_adapter(ProviderConfig(provider_mode="mock"))
assert "shopping_search" not in create_default_registry(ProviderConfig(provider_mode="mock")).list()
```

- [ ] **Step 2: 运行测试验证失败**

Expected: FAIL，factory 仍返回 `MockProductSearchAdapter` / `MockPriceCompareAdapter`。

- [ ] **Step 3: 移除默认 mock adapter 创建**

adapter factory 在非真实配置时 fail closed；`ShoppingSearchTool` 构造器要求显式传入 search/compare adapter。测试 fake 保留在测试文件，不放入生产 Plugin 装配路径。

- [ ] **Step 4: 同步模型契约和文档**

`CapabilityContract.input_requirements` 改为 `needs`；`.env.example` 明确 mock mode 不注册购物工具；权威文档只描述一个 real-only `shopping_search`。

- [ ] **Step 5: 完整定向验证**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/shopping \
  tests/integration/tools \
  tests/integration/runtime/test_runtime_extended_behaviors.py

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/tools/plugins/builtin/shopping \
  tests/integration/shopping
```

Expected: 全部通过；Registry 与代码搜索均不存在 `shopping_list_search`；运行态不会构造 mock shopping adapter。
