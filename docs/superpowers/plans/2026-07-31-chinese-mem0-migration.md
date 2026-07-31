# Mem0 全中文记忆与当前用户迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Mem0 后续提取的长期记忆固定使用简体中文，并把当前运行用户 `10086 + agent.default` 的现有英文记忆安全迁移为中文。

**Architecture:** Mem0 sidecar 通过顶层 `custom_instructions` 约束未来 memory text 的语言，不改变 Mem0 的提取、合并、检索和持久化职责。新增 operator migration 模块与 CLI：使用现有 Qwen `ChatAdapter` 翻译、使用 Mem0 REST `PUT /memories/{id}` 原位更新、使用 history 验证可恢复性；默认只检查，真实翻译与更新必须显式开启 real mode 和双重 operator 门禁。

**Tech Stack:** Python 3.12、Mem0 OSS 2.0.11、Qwen OpenAI-compatible Chat Completions、FastAPI、pytest

## Global Constraints

- 迁移范围固定为运行时 `user_id=10086`、`agent_id=agent.default` 映射后的 Mem0 身份，不扫描其他用户。
- 不在 renderer 或每轮 Agent prompt 中翻译记忆。
- 不写入记忆正文、Provider 原始响应、API key 或 token 到日志、报告或仓库文件。
- 默认命令不调用 Provider、不更新 Mem0。
- 实际迁移必须同时满足 `MULTIMODAL_AGENT_PROVIDER_MODE=real`、完整 Qwen/Mem0 配置、`--apply` 和 `--allow-real-provider`。
- 翻译结果必须非空、包含中文，并保留原文中的 URL 与数字 token；失败时不更新该条并停止。
- 更新后读取 memory 与 history，确认新文本已生效且 history 含旧值；session snapshot 仍需新建 session 才刷新。
- pytest 只使用 fake adapter/transport 和 mock mode，不调用真实 Provider 或 Mem0。

---

### Task 1: 配置 Mem0 未来记忆使用简体中文

**Files:**
- Modify: `docker/mem0/mem0_env.py`
- Modify: `docker/mem0/mem0_sidecar.py`

**Interfaces:**
- Produces: `CHINESE_MEMORY_CUSTOM_INSTRUCTIONS: str`
- Consumes: `Memory.from_config({"custom_instructions": ...})`

- [ ] **Step 1: 最小实现**

在 `mem0_env.py` 定义中文 extraction instruction，并在 `_build_memory()` 的顶层 config 传入：

```python
"custom_instructions": CHINESE_MEMORY_CUSTOM_INSTRUCTIONS
```

按照 `tests/README.md` 不为 prompt 文案或配置常量新增 pytest；结构是否被 Mem0 2.0.11 接受由
Task 3 的 sidecar rebuild/healthcheck 验证。

### Task 2: 实现当前用户存量记忆迁移

**Files:**
- Create: `src/assistant_agent/memory/mem0/chinese_migration.py`
- Create: `scripts/migrate_mem0_memories_to_chinese.py`
- Test: `tests/tdd/chinese-mem0-migration/test_chinese_memory_migration.py`

**Interfaces:**
- Produces: `ChineseMemoryMigrationReport`
- Produces: `migrate_memories_to_chinese(...)`
- Consumes: `RequestIdentity`、`bind_mem0_identity`、`ChatAdapter`、`Mem0Transport`

- [ ] **Step 1: 写身份隔离和 inspect 模式失败测试**

使用 fake transport 返回两条当前用户记忆，断言只发出带不透明 `user_id + agent_id` 的 GET；`apply=False` 时不调用 translator、不发 PUT。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/chinese-mem0-migration/test_chinese_memory_migration.py
```

Expected: FAIL，因为 migration 模块尚不存在。

- [ ] **Step 3: 实现 inspect 与筛选**

实现 bounded list、中文检测、候选统计和内容无关的结构化 report。

- [ ] **Step 4: 写 apply 失败测试**

fake translator 返回中文；断言每条候选依次执行 PUT、GET、history，并验证 URL、数字与旧值 history。已是中文的记忆跳过。

- [ ] **Step 5: 实现 apply 和翻译校验**

使用 `ChatRequest` 明确要求只返回译文；provider error、空文本、无中文、URL/数字丢失或 history 缺旧值时停止并返回稳定 error code。

- [ ] **Step 6: 实现 CLI 门禁**

CLI 参数：

```text
--user-id 10086
--agent-id agent.default
--env-file .env
--apply
--allow-real-provider
```

默认无 `--apply` 时只列出数量；apply 时强制 real mode、Qwen adapter 和已配置 Mem0，输出只含计数、memory ID 和状态。

- [ ] **Step 7: 运行 GREEN**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/chinese-mem0-migration
```

Expected: 全部 PASS。

### Task 3: 文档、构建与真实迁移

**Files:**
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/CONTEXT_ENGINEERING_STATUS.md`
- Modify: `scripts/README.md`

**Interfaces:**
- Consumes: Task 1/2 的稳定 operator 流程
- Produces: 当前中文记忆与迁移 runbook

- [ ] **Step 1: 同步权威文档**

记录 `custom_instructions` 中文约束、存量迁移门禁、原位 update/history 与新 session 生效边界。

- [ ] **Step 2: 运行 mock/offline 验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/chinese-mem0-migration
```

- [ ] **Step 3: 重建并重启本地 Mem0 sidecar**

仅使用本地 Dockerfile 与固定依赖版本，保留现有 Qdrant/history volumes；若构建需要联网拉取依赖则停止并报告。

- [ ] **Step 4: inspect 当前用户**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/migrate_mem0_memories_to_chinese.py --user-id 10086
```

确认范围与候选数量，不调用 Provider。

- [ ] **Step 5: 执行真实迁移**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/migrate_mem0_memories_to_chinese.py \
  --user-id 10086 \
  --apply \
  --allow-real-provider
```

逐条翻译、更新、GET/history 验证；遇错停止。

- [ ] **Step 6: 最终验证**

重新 inspect，确认英文候选为 0；运行默认 core pytest 和 `git diff --check`。报告真实 Provider 调用条数、迁移成功/失败数量、是否需要新 session。
