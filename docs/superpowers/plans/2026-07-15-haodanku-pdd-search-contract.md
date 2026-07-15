# Haodanku Platform Search Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align real JD/PDD pagination and LLM platform aliases with the provider contract, then re-verify the shopping Gateway loop.

**Architecture:** Keep the provider-neutral `top_k` contract. Normalize JD/PDD request sizes and Chinese platform aliases at the Haodanku adapter boundary, then preserve the existing per-platform truncation and ranking logic.

**Tech Stack:** Python 3.12, urllib, Pydantic, pytest, FastAPI Gateway WebSocket.

## Global Constraints

- Default tests remain mock/fake and offline.
- Real calls require `MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke`.
- Do not bypass validator, executor, registry, policy, or audit boundaries.
- Do not attempt to bypass missing JD account authorization.

---

### Task 1: Correct platform search contracts

**Files:**
- Modify: `tests/test_multiplatform_haodanku.py`
- Modify: `src/assistant_agent/providers/haodanku_product_search.py`
- Modify: `haodanku-openapi-docs/interfaces/商品接口.md`

**Interfaces:**
- Consumes: `build_haodanku_platform_search_url(..., platform: str, limit: int) -> str`
- Produces: JD/PDD URLs containing `back=<normalized>` and no `limit`.

- [x] **Step 1: Write the failing URL contract test**

```python
def test_pdd_search_url_normalizes_top_k_to_supported_back():
    url = build_haodanku_platform_search_url(
        base_url="https://v3.api.haodanku.com",
        api_key="key",
        platform="pdd",
        keyword="蓝牙耳机",
        limit=3,
    )
    assert "back=10" in url
    assert "limit=" not in url
```

- [x] **Step 2: Run the test and verify RED**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_multiplatform_haodanku.py::test_pdd_search_url_normalizes_top_k_to_supported_back -q`

Expected: FAIL because the URL currently contains `limit=3`.

- [x] **Step 3: Implement the smallest adapter fix**

Add a PDD page-size normalizer that clamps and rounds upward to a multiple of ten, and make the URL builder emit `back` for PDD.

- [x] **Step 4: Update the local provider document**

Document `back` as one of `10,20,...,100`, removing the stale `limit` field.

- [x] **Step 5: Add JD and Chinese-platform regression coverage**

Real probing exposed JD's allowed `back` values and Chinese platform values emitted by DeepSeek. Add RED/GREEN tests for both search and compare normalization.

- [x] **Step 6: Run focused offline verification**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_multiplatform_haodanku.py tests/test_haodanku_product_search_adapter.py tests/test_price_compare_shopping_ranking.py tests/test_realtime_agent_backend.py -q`

Expected: all selected tests pass.

### Task 2: Real provider and Gateway verification

**Files:**
- No repository writes; use local untracked `.env` only.

**Interfaces:**
- Consumes: `provider_smoke` DeepSeek and Haodanku configuration.
- Produces: observed platform coverage, governed tool sequence, one shopping detail chunk, and terminal `run.end`.

- [x] **Step 1: Run a real adapter probe**

Run a bounded `ProductSearchRequest(query="蓝牙耳机", top_k=3)` and `PriceCompareRequest(..., top_k=9)`; report only counts, platform statuses, error codes, prices, and link statuses.

- [x] **Step 2: Run the real Gateway smoke**

Start `scripts/run_server.py` with DeepSeek, mock image provider, and one trial user; send one shopping comparison turn through `scripts/run_gateway_client.py`.

- [x] **Step 3: Verify terminal invariants**

Confirm DeepSeek selected `product_search` then `price_compare`, both crossed governed tool lifecycle events, the client received exactly one shopping `stream.chunk`, and `run.end.reason` is `completed`.

- [x] **Step 4: Commit the phase**

Stage only the spec, plan, adapter, focused tests, and local API document. Commit with `fix: align pdd search with real provider contract`.
