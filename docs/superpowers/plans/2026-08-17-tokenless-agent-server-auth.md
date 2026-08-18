# Agent Server 完全免密实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Agent Server 在 mock 与 real 模式下都无需 service token，并继续用 `X-Assistant-User` 构造请求身份。

**Architecture:** 保留 Agent Server auth hook、owner filter 和 Store namespace 隔离逻辑，但认证 hook 不再校验 Bearer token 或 HMAC。CLI 不再接受 `--token`，配置与 authority 删除 service-token 要求；任何能访问端口的客户端均可声明身份，这是用户明确接受的新安全边界。

**Tech Stack:** Python 3.12、LangGraph SDK Auth、pytest。

## Global Constraints

- mock 与 real 的认证行为一致：`X-Assistant-User` 存在时作为 identity，否则使用 `local-developer`。
- 不读取 `ASSISTANT_AGENT_SERVER_SERVICE_TOKEN`，不校验 Authorization 或签名。
- 保留 thread owner filter、Store namespace scope 和 custom route auth hook。
- 更新 `IDENT-001` 的稳定身份契约和既有 core 测试。
- 测试必须 mock/local/offline，不调用真实 Provider。

---

### Task 1: 用 RED 固定免密身份契约

**Files:**
- Modify: `tests/core/contract/test_gateway_contract.py`
- Modify: `tests/core/INVARIANTS.md`

**Interfaces:**
- Consumes: `authenticate(authorization, headers)`。
- Produces: real 模式下无 token、无签名仍返回由 `X-Assistant-User` 指定的 authenticated principal。

- [ ] **Step 1: 增加 `IDENT-001` 行为测试**

  设置 real mode 和无关 service token，传入无效 Authorization 且不传签名，断言 identity 等于 header sentinel。

- [ ] **Step 2: 运行测试并确认 RED**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/contract/test_gateway_contract.py
  ```

  预期旧实现抛出 `HTTPException`，失败原因是仍校验 token。

### Task 2: 最小化认证与 CLI

**Files:**
- Modify: `src/assistant_agent/agent_server/auth.py`
- Modify: `scripts/agent_cli.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: 请求 header `X-Assistant-User`。
- Produces: mode-agnostic、tokenless 的 Agent Server principal；CLI 只发送 identity header。

- [ ] **Step 1: 简化认证 hook**

  删除 mode/token/HMAC 分支，始终返回 `identity`、`permissions=["assistant:developer"]` 与 `is_authenticated=True`。

- [ ] **Step 2: 移除 CLI token 参数与签名生成**

  删除 `--token`、Bearer/header 签名逻辑及已无调用方的签名 helper。

- [ ] **Step 3: 运行测试并确认 GREEN**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/contract/test_gateway_contract.py
  ```

### Task 3: 同步 authority 并验证运行入口

**Files:**
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`

**Interfaces:**
- Consumes: Task 2 的免密认证行为。
- Produces: 与源码一致的当前安全边界说明。

- [ ] **Step 1: 更新 auth 与媒体认证说明**

  明确所有 mode 均免密、identity 可由客户端声明，并记录非受信网络暴露风险。

- [ ] **Step 2: 运行完整定向验证**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/contract/test_gateway_contract.py
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agent_cli.py --help
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
  ```

### Task 4: 删除目标英文记忆

**Files:**
- Modify: Agent Server Store 中唯一精确匹配的 item。

**Interfaces:**
- Consumes: `http://127.0.0.1:8089` Store API 与目标英文正文。
- Produces: 目标 item 不再存在，其他 item 数量保持不变。

- [ ] **Step 1: 重启服务以加载认证变更**

  只在能明确识别用户当前 `8089` 进程时停止并按原命令重启。

- [ ] **Step 2: 精确查询与删除**

  对候选本地 identity 查询完整正文；只有唯一匹配时调用 `delete_item(namespace, key)`。

- [ ] **Step 3: 删除后复查**

  完整正文匹配数为 0，非目标 item 数量与删除前一致。
