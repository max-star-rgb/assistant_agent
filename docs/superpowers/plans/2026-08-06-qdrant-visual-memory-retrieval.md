# Qdrant 视觉文本记忆检索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用本地 Qdrant 的 BM25 + BGE 混合检索替代视觉历史的 SigLIP2 text-text 检索，修复明确物体文本漏召回。

**Architecture:** 新增独立的视觉文本索引协议和 Qdrant adapter，VLM 成功后将文本时间线写入派生索引；工具查询 Qdrant 并把命中 payload 投影为带时间标签的文本。实时本地 semantic store 保持事实来源，检索后端故障显式返回 unavailable。

**Tech Stack:** Python 3.11、Pydantic、Qdrant REST >= 1.17、FastEmbed、BAAI/bge-small-zh-v1.5、Qdrant server-side BM25 multilingual、pytest。

## Global Constraints

- Dense 模型固定为 `BAAI/bge-small-zh-v1.5`。
- BM25:dense Weighted RRF 权重固定为 `3:1`，prefetch 各 32，最终返回 12。
- real mode 禁止在 Qdrant 故障时静默回退 SigLIP2 text-text。
- 所有 pytest 使用 mock/offline，不访问网络或真实 Provider。
- 不回滚工作区已有的视觉时间标签改动。

---

### Task 1: 定义索引边界与事故回归

**Files:**
- Create: `src/assistant_agent/media/video/visual_memory_index.py`
- Create: `tests/tdd/qdrant-visual-memory-search/test_visual_memory_hybrid_retrieval.py`

**Interfaces:**
- Produces: `VisualMemoryIndexDocument`、`VisualMemoryIndexQuery`、`VisualMemoryIndexHit`、`VisualMemoryIndexResult`、`VisualMemoryTextIndex`。

- [ ] **Step 1: 写失败测试**：用事故中的 90 条文本 fixture 构造文档，断言查询“鼠标”时 Seq81-85 至少一条进入 Top 3，并断言 user/session/time filter。
- [ ] **Step 2: 运行测试确认 RED**：`MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/qdrant-visual-memory-search`，预期因索引协议或 adapter 尚不存在而失败。
- [ ] **Step 3: 实现最小 Pydantic 契约与显式 unavailable adapter**，使上层可以在不导入第三方依赖时表达成功和故障。
- [ ] **Step 4: 再次运行定向测试并确认契约测试通过，真实混合召回测试仍因 adapter 缺失失败。**

### Task 2: 实现 Qdrant 混合检索 adapter

**Files:**
- Create: `src/assistant_agent/media/video/qdrant_visual_memory_index.py`
- Modify: `pyproject.toml`
- Modify: `docker/mem0/compose.yaml`
- Test: `tests/tdd/qdrant-visual-memory-search/test_visual_memory_hybrid_retrieval.py`

**Interfaces:**
- Consumes: Task 1 的索引契约。
- Produces: `QdrantVisualMemoryTextIndex.upsert/search/delete_session/delete_user/close`。

- [ ] **Step 1: 写 adapter 查询构造失败测试**：断言 named dense/sparse vectors、严格 payload filter、prefetch=32、limit=12 和 Weighted RRF weights `[3, 1]`。
- [ ] **Step 2: 运行测试确认 RED。**
- [ ] **Step 3: 添加 FastEmbed 可选依赖并实现 adapter**；BGE 使用本地 cache 与禁止下载配置，Qdrant REST collection 使用 dense cosine 与服务端 BM25 sparse vector，避免 FastEmbed BM25 缺失 multilingual tokenizer。
- [ ] **Step 4: 使用受控本地 Qdrant 运行事故 fixture，确认“鼠标”命中 Top 3。**
- [ ] **Step 5: 把 compose Qdrant 升级到 >=1.17，并开放仅 localhost 的端口。**

### Task 3: 接入 VLM 发布和 Tool 查询链路

**Files:**
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/media/embedding/consumers/object_search.py`
- Modify: `src/assistant_agent/tools/plugins/contracts.py`
- Modify: `src/assistant_agent/tools/plugins/registry_factory.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Test: `tests/tdd/qdrant-visual-memory-search/test_visual_memory_hybrid_retrieval.py`

**Interfaces:**
- Consumes: `VisualMemoryTextIndex`。
- Produces: VLM 文本完成即 upsert；tool 仅查询 Qdrant 并输出时间戳文本。

- [ ] **Step 1: 写失败测试**：断言 VLM 发布不再调用 `embed_text`，新完成的高序号帧不等待低序号帧即可进入索引，tool 不依赖 embedding coordinator。
- [ ] **Step 2: 运行测试确认 RED。**
- [ ] **Step 3: 注入 runtime-owned index，替换发布与查询路径，并保留本地时间线写入。**
- [ ] **Step 4: 实现 structured unavailable，验证无 SigLIP2 fallback。**
- [ ] **Step 5: 运行完整 feature TDD 目录确认 GREEN。**

### Task 4: 生命周期、配置和文档同步

**Files:**
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/runtime/assistant_runtime_app.py`
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `.env.example`

**Interfaces:**
- Produces: Qdrant URL、collection、model cache、timeout 配置；session/user 清理和关闭生命周期。

- [ ] **Step 1: 写配置解析与清理失败测试。**
- [ ] **Step 2: 运行测试确认 RED。**
- [ ] **Step 3: 增加配置和生命周期接线，real mode 缺失本地模型或 Qdrant 时保持显式 unavailable。**
- [ ] **Step 4: 同步两份权威文档和示例环境变量。**
- [ ] **Step 5: 运行 feature TDD，确认 GREEN。**

### Task 5: 验证与提交

**Files:**
- Verify: `tests/tdd/qdrant-visual-memory-search/`
- Verify: target source files and authority docs

**Interfaces:**
- Produces: 可复现验证证据和仅包含本任务文件的提交。

- [ ] **Step 1: 运行 feature TDD 最小集合。**
- [ ] **Step 2: 运行相关既有 visual-memory TDD，确认时间标签和 compaction 未回归。**
- [ ] **Step 3: 检查 `git diff --check`、依赖锁定、Qdrant 版本和无 runtime download。**
- [ ] **Step 4: 只暂存本任务文件并提交；不包含用户已有的无关改动。**
