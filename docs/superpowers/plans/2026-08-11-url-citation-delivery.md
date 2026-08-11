# 联网来源角标交付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 DashScope 结构化来源以 OpenAI 风格 URL citation annotations 交付到 `/agent/run` 与 Gateway `run.end`，正文保持不变。

**Architecture:** 新增 Runtime-owned citation 模型和纯解析函数；assistant loop 在 Provider 终态构造 annotations，随后 API/Gateway 仅做薄投影。流式正文、Media-Agent vendor 协议和 conversation history 不变。

**Tech Stack:** Python 3.11、Pydantic v2、pytest、FastAPI/Gateway 既有模型。

## Global Constraints

- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 Provider。
- 不新增依赖，不改写正文 Markdown，不追加底部来源列表。
- offsets 使用 Unicode code point 半开区间。
- 不修改 Media-Agent `/agent-service/v1` wire contract。
- 保留工作区既有改动；本轮不自动提交，避免把重叠的用户改动收入同一 commit。

---

### Task 1: Runtime citation 公共模型与解析器

**Files:**
- Create: `src/assistant_agent/runtime/citations.py`
- Create: `tests/tdd/url-citation-delivery/test_citations.py`

**Interfaces:**
- Consumes: `ProviderSearchSource(index: int, title: str, url: str)`。
- Produces: `UrlCitationAnnotation` 与 `build_url_citation_annotations(response_text: str, sources: list[ProviderSearchSource]) -> list[UrlCitationAnnotation]`。

- [ ] **Step 1: 写失败测试**

```python
def test_build_annotations_maps_each_cited_occurrence_without_rewriting_text() -> None:
    text = "杭州 [5]，苏州 [ref_2]，再次杭州 [5]。"
    annotations = build_url_citation_annotations(text, sources)
    assert [text[item.start_index:item.end_index] for item in annotations] == ["[5]", "[ref_2]", "[5]"]
    assert [item.source_id for item in annotations] == ["source_5", "source_2", "source_5"]
```

另加一个用例断言未匹配、未引用、`[1](url)`、`[[1]]`、`[0]` 与非 HTTP(S) URL不产生 annotation。

- [ ] **Step 2: 运行测试确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/url-citation-delivery/test_citations.py`

Expected: collection/import FAIL，因为 `assistant_agent.runtime.citations` 尚不存在。

- [ ] **Step 3: 最小实现模型与解析器**

```python
class UrlCitationAnnotation(BaseModel):
    type: Literal["url_citation"] = "url_citation"
    start_index: int = Field(ge=0)
    end_index: int = Field(gt=0)
    source_id: str = Field(pattern=r"^source_[1-9][0-9]*$")
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)

def build_url_citation_annotations(
    response_text: str,
    sources: Sequence[ProviderSearchSource],
) -> list[UrlCitationAnnotation]:
    by_index = {
        source.index: source
        for source in sources
        if urlparse(source.url).scheme.lower() in {"http", "https"}
        and bool(urlparse(source.url).netloc)
    }
    annotations = []
    for match in _CITATION_PATTERN.finditer(response_text):
        index = int(match.group("index"))
        source = by_index.get(index)
        if source is None:
            continue
        annotations.append(UrlCitationAnnotation(
            start_index=match.start(),
            end_index=match.end(),
            source_id=f"source_{index}",
            title=source.title,
            url=source.url,
        ))
    return annotations
```

解析器按正文顺序扫描 `[n]` / `[ref_n]`，按 source index 查找，只接受绝对 HTTP(S) URL并保留原文本 offset。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/url-citation-delivery/test_citations.py`

Expected: PASS。

### Task 2: Assistant Runtime 与 HTTP `/agent/run` 终态投影

**Files:**
- Modify: `src/assistant_agent/runtime/output_models.py`
- Modify: `src/assistant_agent/runtime/requests.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/api/models.py`
- Create: `tests/tdd/url-citation-delivery/test_runtime_http_projection.py`

**Interfaces:**
- Consumes: `build_url_citation_annotations()` 与 `UrlCitationAnnotation`。
- Produces: `AssistantTextOutput.annotations`、`AgentResponse.annotations`、`AgentRunResponse.annotations`。

- [ ] **Step 1: 写失败测试**

```python
def test_native_provider_citations_reach_http_response() -> None:
    decision = _native_final_decision(ChatResult(
        response_text="answer [1]",
        provider="qwen",
        search_sources=[ProviderSearchSource(index=1, title="source", url="https://example.com/1")],
    ))
    state = AgentState.from_request(request, run_id="run-sentinel", trace_id="trace-sentinel")
    state.set_response(AgentResponse(message=decision.text, annotations=decision.annotations))
    response = agent_run_response_from_state(state)
    assert response.annotations[0].url == "https://example.com/1"
```

再覆盖 direct-chat 与 tool 后 final-answer 的两个持久化分支，防止只在一个 assistant loop 分支保留 annotations。

- [ ] **Step 2: 运行测试确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/url-citation-delivery/test_runtime_http_projection.py`

Expected: FAIL，因为输出模型尚无 `annotations` 或终态映射为空。

- [ ] **Step 3: 最小实现 Runtime 与 HTTP 投影**

给三个响应模型增加 `annotations: list[UrlCitationAnnotation] = Field(default_factory=list)`；`_native_final_decision()` 和 direct-chat 结果调用唯一解析函数；所有写入 `AgentResponse` 的真实 Provider 终态分支复制 annotations；`agent_run_response_from_state()` 原样复制。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/url-citation-delivery/test_runtime_http_projection.py`

Expected: PASS。

### Task 3: Gateway 终态投影、权威文档与整体验证

**Files:**
- Modify: `src/assistant_agent/gateway/runtime_types.py`
- Modify: `src/assistant_agent/gateway/runtime_adapter.py`
- Modify: `src/assistant_agent/gateway/session.py`
- Create: `tests/tdd/url-citation-delivery/test_gateway_projection.py`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/gateway-architecture.md`

**Interfaces:**
- Consumes: `AgentResponse.annotations`。
- Produces: `RealtimeAgentResult.annotations` 与 completed `run.end.payload.annotations`。

- [ ] **Step 1: 写失败测试**

```python
def test_run_end_delivers_terminal_annotations() -> None:
    payload = _run_end_payload(
        result=RealtimeAgentResult(status="completed", response_text="answer [1]", annotations=[annotation]),
        expects_reply=False,
        run_id="run-sentinel",
    )
    assert payload["annotations"] == [annotation.model_dump(mode="json")]
```

另断言 cancelled/error 终态不交付 annotations，且空列表不增加 payload 字段。

- [ ] **Step 2: 运行测试确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/url-citation-delivery/test_gateway_projection.py`

Expected: FAIL，因为 `RealtimeAgentResult` 与 `run.end` 尚未投影 annotations。

- [ ] **Step 3: 最小实现 Gateway 投影并同步 authority**

`GatewayRuntimeAdapter` 从最终 `AgentResponse` 复制 annotations；`_run_end_payload()` 仅在 completed 且非空时序列化。更新两份 authority，明确 terminal enrichment、offset 单位、旧客户端兼容和 Media-Agent 非本轮范围。

- [ ] **Step 4: 运行 feature 与文档验证**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/url-citation-delivery tests/tdd/provider-search-provenance
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent
```

Expected: 全部 exit 0；pytest 无真实网络调用。
