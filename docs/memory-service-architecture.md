# LangGraph-native 长期记忆架构

最后更新：2026-08-14

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 父图固定 Memory 节点、冻结快照与后端协议的当前权威 |
| Owns | `MemoryBackend`、`memory_recall`、`memory_commit`、`memory_context`、Mem0/LangMem/第三方装配 |
| Does not own | Mem0 HTTP wire、prompt 渲染、Agent Server Store 实现、旧 Memory bundle/ledger |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/memory.py`、`src/assistant_agent/memory/mem0/` |
| 验证入口 | `docs/authority.toml` 中 `graph-memory.verification`；核心不变量 `MEMORY-001` |
| 相邻 authority | Mem0 wire 见 [`memory_server_api_spec.md`](memory_server_api_spec.md)；Context 见 [`context_engineering_status.md`](context_engineering_status.md) |

## 固定父图节点

长期记忆只在父图执行：

```text
memory_recall -> fast | planning -> memory_commit
```

planning worker 只读取父图冻结的 `memory_context`，不持有 backend，也不重复 recall/commit。recall 使用
LangGraph `RetryPolicy(max_attempts=3)`；最终失败由原生 error handler 写入
`memory_context=()`、`memory_status=degraded`，随后继续回答。commit 异常 fail-open，不删除、不替换最终
`AIMessage`，第三方原始错误不进入 state。

## 最小 backend 协议

`MemoryBackend` 只有异步 `recall` 与 `commit`。两者接收受信 `AssistantRunContext`、Agent Server
`thread_id/run_id`、标准 messages 和 `runtime.store`。第三方记忆服务只需实现该协议，不需要继承项目 SDK、
创建 session host 或提供通用 CRUD。

当前装配：

- `disabled`：离线默认，召回为空、提交跳过；
- `mem0`：复用薄 `Mem0Client`；身份通过 opaque binding，提交的 `source_turn` 优先使用 Agent Server
  `run_id`，本地 Graph 无 run ID 时使用 thread ID；不构造旧 SQLite commit ledger；
- `langmem`：使用官方 manager，召回只访问 compiled graph 注入的同一个 `runtime.store`；
- custom：composition 可注入任何满足 `MemoryBackend` 的第三方 adapter。

mock mode 只能使用 disabled；远端 backend 要求 real mode 和完整显式配置，不能探测 key 后启用，也不能静默
回退。

## 冻结快照与安全

state 只保存有界 `tuple[str, ...]` 与 `ready|empty|degraded`。最多 32 项、每项 4,000 字、总计 12,000 字。
Memory 正文是不可信历史数据，由 dynamic prompt 放入明确的 untrusted/frozen 数据边界，不能覆盖当前请求、
身份、权限或 Tool schema。

旧 `MemoryNodeBundle`、commit ledger 与复杂 time-travel 语义仍供旧外围 runtime 使用；生产
`assistant-native-v1` 不导入它们。新旧 checkpoint 不做 schema 迁移。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_memory_lifecycle.py \
  tests/tdd/native-agent-parent-graph/test_native_memory.py
```
