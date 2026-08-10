# Tool 预算与 DashScope 搜索来源实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让旅行任务在受控预算内完成 `POI -> lodging_search` 链路，并让百炼原生联网搜索的实际来源成为可交付、可观测的结构化证据。

**Architecture:** 保持 `AgentGraphRuntime`、RunToolCatalog 和 Tool 治理链不变。Runtime 将 Skill 加载类 control Tool 与业务 action Tool 分账；MCP adapter 只对高德文本 POI 做带覆盖计数的模型观察投影；Qwen/百炼主 Chat 可配置为 DashScope HTTP adapter，由 Provider 边界解析文本、Function Calling、usage 和 `search_info`，再归一化到现有 `ChatResult`。

**Tech Stack:** Python 3.12、Pydantic、标准库 `urllib`、pytest、现有 `LLMEvent/ChatResult` 与 TraceEvent。

## Global Constraints

- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，本计划不调用真实 Provider。
- 不新增或安装 `dashscope`、`requests`、`httpx` 等依赖。
- 酒店候选、报价、库存和 OTA 链接仍只能来自 `lodging_search`。
- Provider source URL 只进入最终来源列表和显式本地协议 capture；canonical trace 仅记录是否搜索和来源数量。
- 不回滚当前工作区已有改动；计划文件不提交。

---

### Task 1: 业务 Tool 与 control Tool 分账

**Files:**
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Test: `tests/tdd/tool-budget-governance/test_control_action_budget.py`

**Interfaces:**
- Consumes: `LOAD_SKILL_TOOL_NAME`、`LOAD_SKILL_REFERENCE_TOOL_NAME`、`AssistantLoopState.pending_tool_calls`。
- Produces: `ProviderConfig.max_tool_iterations=8`、`ProviderConfig.max_control_tool_iterations=3`、graph state 的 `action_tool_calls_used/control_tool_calls_used/tool_calls_used`。

- [x] **Step 1: 写失败测试**

  覆盖默认 action 预算为 8；一次 `load_skill` 不消耗 action 预算；control 达到 3 后不能继续执行 control Tool；action 达到 8 后仍进入 `FINALIZE`。

- [x] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/tool-budget-governance`

  Expected: 因缺少分账字段和默认值仍为 5 而失败。

- [x] **Step 3: 最小实现**

  在配置中读取 `MAX_TOOL_ITERATIONS`（默认 8）和 `MAX_CONTROL_TOOL_ITERATIONS`（默认 3）。将 `load_skill/load_skill_reference` 分类为 control；其他 Tool 分类为 action。执行批次前分别检查额度，执行后分别累计，同时保留 `tool_calls_used` 总数供兼容诊断。

- [x] **Step 4: 运行 GREEN**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/tool-budget-governance tests/tdd/tool-repeat-policy`

  Expected: PASS。

### Task 2: 高德文本 POI 模型观察投影

**Files:**
- Create: `src/assistant_agent/mcp/model_observation.py`
- Modify: `src/assistant_agent/mcp/sdk_client.py`
- Test: `tests/tdd/mcp-observation-projection/test_amap_text_search_projection.py`

**Interfaces:**
- Consumes: `server_name`、`tool_name`、MCP `structuredContent` 或 JSON text summary。
- Produces: `project_mcp_model_observation(...) -> dict[str, Any]`，高德文本搜索返回 `pois/total_count/returned_count/truncated`。

- [x] **Step 1: 写失败测试**

  构造 20 个含图片的高德 POI，断言模型观察只保留前 5 个的 `id/name/address/location/typecode`，移除 `photos`，并显式报告 `total_count=20`、`returned_count=5`、`truncated=true`；非目标 MCP 仍保持原投影。

- [x] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/mcp-observation-projection`

  Expected: 当前 observation 保留完整 20 条和图片字段而失败。

- [x] **Step 3: 最小实现**

  在 MCP client 的模型投影边界调用专项 projector；完整 `ToolResult.data` 不变。无法识别结构时退回现有通用 sanitizer，不猜测或丢弃未知业务结果。

- [x] **Step 4: 运行 GREEN**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/mcp-observation-projection tests/tdd/mcp-tool-failure-recovery`

  Expected: PASS。

### Task 3: DashScope-native Chat 与联网来源

**Files:**
- Create: `src/assistant_agent/providers/dashscope_chat.py`
- Modify: `src/assistant_agent/runtime/chat_adapter.py`
- Modify: `src/assistant_agent/providers/llm_events.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Test: `tests/tdd/provider-search-provenance/test_dashscope_chat_adapter.py`

**Interfaces:**
- Consumes: `ChatRequest`、百炼 `/api/v1/services/aigc/text-generation/generation` 响应。
- Produces: `DashScopeChatAdapter.chat(request) -> ChatResult`；`ChatResult.search_sources: list[ProviderSearchSource]`；canonical `llm.chat.finished` 属性 `search_performed/search_source_count`。

- [x] **Step 1: 写失败测试**

  使用注入的离线 HTTP transport，断言请求把 messages 放进 `input`，把 tools、tool_choice、`enable_search`、`enable_source`、`enable_citation` 放进 `parameters`；响应能解析 text、tool calls、usage、request ID 和去重后的来源 URL；最终文本附带来源链接；canonical trace 不包含 URL。

- [x] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/provider-search-provenance`

  Expected: adapter、来源模型和 trace 属性尚不存在而失败。

- [x] **Step 3: 最小实现**

  使用标准库 HTTP POST；端点由兼容 URL 或显式 `QWEN_CHAT_DASHSCOPE_BASE_URL` 推导。只接受 HTTP(S) 来源并做长度限制、URL 去重。`create_chat_adapter()` 在 qwen 且 `QWEN_CHAT_API_PROTOCOL=dashscope` 时选择新 adapter，保留 `openai_compatible` 回退。默认协议改为 `dashscope`，不改变其他 Provider。

- [x] **Step 4: 运行 GREEN**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/provider-search-provenance tests/tdd/deep-research-mode`

  Expected: PASS。

### Task 4: Authority 与最终验证

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/context_engineering_status.md`

**Interfaces:**
- Consumes: Tasks 1-3 的最终结构化契约。
- Produces: 当前 authority 对预算分账、MCP 专项投影和 DashScope 来源边界的准确说明。

- [x] **Step 1: 更新 owner authority**

  删除“当前只能使用 OpenAI-compatible、来源不可验证”的过期描述，记录 DashScope-native 的配置、来源投影与 prompt-safe trace 规则；记录 action/control 预算和 POI 覆盖计数。

- [ ] **Step 2: 运行全部定向验证**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/tool-budget-governance tests/tdd/tool-repeat-policy tests/tdd/mcp-observation-projection tests/tdd/mcp-tool-failure-recovery tests/tdd/provider-search-provenance tests/tdd/deep-research-mode tests/tdd/travel_tool_orchestration`

  Expected: PASS。

- [x] **Step 3: 验证文档权威与 diff**

  Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .`

  Run: `git diff --check`

  Expected: 两条命令 exit 0。

- [x] **Step 4: 最终复核**

  确认未调用真实 Provider、未新增依赖、未输出凭据、未修改无关文件，并按 `Core invariant` / `Tests` 格式汇报。
