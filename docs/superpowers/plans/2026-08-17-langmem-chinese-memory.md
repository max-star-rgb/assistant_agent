# LangMem 中文记忆实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LangMem 新增或更新的长期记忆正文默认使用简体中文，并精确删除用户指定的现有英文记忆。

**Architecture:** 保持 `MemoryBackend`、Store namespace、召回与后台 debounce 流程不变，只在 LangMem manager 装配时传入中文提取指令。已有英文记忆通过 Agent Server Store 的精确 namespace/key 删除，不开启模型的全局删除能力。

**Tech Stack:** Python 3.12、LangMem `create_memory_store_manager`、LangGraph Store、pytest。

## Global Constraints

- 默认使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock` 验证，不调用真实 Provider。
- 记忆正文必须使用简体中文；代码、协议字段、产品名和其他专有名词可保留原文。
- 不改变 `memory_context` 的结构、namespace、召回上限、debounce 或后台 Memory Graph。
- 不为 prompt 文案新增 pytest；运行现有 Memory 定向回归。
- 只删除与用户所贴英文内容精确匹配的 Store item，不删除其他记忆。

---

### Task 1: 配置中文 LangMem 提取指令

**Files:**
- Modify: `src/assistant_agent/native_agent/memory.py`
- Modify: `docs/memory-service-architecture.md`

**Interfaces:**
- Consumes: `langmem.create_memory_store_manager(model, *, instructions, namespace, store)`。
- Produces: `_create_langmem_manager(...)` 创建的 manager，其非结构化记忆正文遵守简体中文约束。

- [ ] **Step 1: 在 Memory 模块增加中文提取指令常量**

  指令保留现有 LangMem 的提取、比较、合并和置信度语义，并增加明确语言约束。

- [ ] **Step 2: 装配 manager 时显式传入 `instructions`**

  在 `create_manager(...)` 调用中传入该常量，不改变其他参数。

- [ ] **Step 3: 同步 Memory authority**

  在 LangMem backend 说明中记录“非结构化记忆正文默认使用简体中文”。

- [ ] **Step 4: 运行静态参数检查**

  使用 Python AST/导入检查确认 manager 调用显式提供 `instructions`，且中文约束存在。

- [ ] **Step 5: 运行 Memory 定向回归与 authority 校验**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/integration/test_memory_lifecycle.py tests/tdd/native-memory-service/test_delayed_extraction.py
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
  ```

### Task 2: 精确删除现有英文记忆

**Files:**
- Modify: Agent Server Store 中一个精确匹配的 item（不修改仓库文件）。

**Interfaces:**
- Consumes: 已认证 Agent Server Store API、目标英文正文。
- Produces: 目标 item 不再可召回，其他 Store item 保持不变。

- [ ] **Step 1: 只读定位目标 item**

  在当前用户可见 namespace 中按完整正文匹配，记录唯一 namespace/key；若匹配数不是 1，停止删除并报告。

- [ ] **Step 2: 精确删除唯一 item**

  调用 `store.delete_item(namespace, key)`，不使用 namespace 批量删除。

- [ ] **Step 3: 复查目标与其他条目**

  重新搜索完整正文应为 0 条，并核对删除前后的非目标条目数量不变。

### Task 3: 复核与交付

**Files:**
- Review: `src/assistant_agent/native_agent/memory.py`
- Review: `docs/memory-service-architecture.md`

**Interfaces:**
- Consumes: Task 1 的代码/文档 diff 与 Task 2 的 Store 删除结果。
- Produces: 完成报告，包含验证命令、Core invariant 决策、测试决策、真实 Provider 调用情况和删除限制。

- [ ] **Step 1: 复核 git diff**

  确认只包含本任务增量，不覆盖工作区既有改动。

- [ ] **Step 2: 判断是否提交**

  工作区已有大量用户改动时不自动提交混合变更；仅在能安全隔离本任务文件/patch 时提交。

- [ ] **Step 3: 汇报结果**

  明确说明没有调用真实 Provider，并按项目格式报告 `Core invariant:` 与 `Tests:`。
