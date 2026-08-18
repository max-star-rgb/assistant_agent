# LangGraph-native 长期记忆架构

最后更新：2026-08-18

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 父图固定 Memory 节点、冻结快照与后端协议的当前权威 |
| Owns | `MemoryBackend`、`memory_recall`、`memory_extract`、`memory_context`、Mem0/LangMem/第三方装配 |
| Does not own | Mem0 HTTP wire、prompt 渲染、Agent Server Store 实现、旧 Memory bundle/ledger |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/memory.py`、`src/assistant_agent/memory/mem0/` |
| 验证入口 | `docs/authority.toml` 中 `graph-memory.verification`；核心不变量 `MEMORY-001` |
| 相邻 authority | Mem0 wire 见 [`memory_server_api_spec.md`](memory_server_api_spec.md)；Context 见 [`context_engineering_status.md`](context_engineering_status.md) |

## Read hot path 与 write cold path

Memory 是领域和 backend protocol 边界，不是必须整体嵌入主图的 compiled subgraph。实际编译拓扑拆成：

```text
assistant-native-v1: capture_trusted_runtime_facts -> memory_recall -> fast/planning -> refresh delayed memory -> END
assistant-memory-v1: memory_extract -> END
```

每个 chat run 都重新 recall 一次，因此能读取 Store 中最新长期记忆；planning worker 只读取当前 run 冻结的
`memory_context`，不持有 backend，也不重复 recall。独立 Memory Graph 使用 message-only state，
不继承父图的 execution、Memory 快照或 fast agent Skill channel。recall 使用
LangGraph `RetryPolicy(max_attempts=3)`；最终失败由 LangGraph 原生 node error handler 返回
`Command(update={memory_context: (), memory_status: degraded}, goto=execution_router)`，父图随后继续回答。
从已完成 recall 之后的 checkpoint resume 时沿用冻结快照；从更早 checkpoint replay 并重新执行 recall 时允许
重新读取最新记忆。其恢复语义与前置 TrustedRuntimeFacts capture 节点一致，二者不建立跨 replay 的额外冻结层。

最终回答产生后，主图使用官方 `langgraph_sdk` 调用 `runs.list(thread_id, status="pending")`，只筛选带
`assistant_agent_run_kind=memory_extraction` metadata 的旧 Memory run，再逐个调用
`runs.cancel(thread_id, run_id, wait=True, action="rollback")`，随后在同一个 conversation thread 上调用
`runs.create` 创建新的
`assistant-memory-v1` delayed run，默认 `after_seconds=1800`、`multitask_strategy="enqueue"`，并写入上述
metadata 标记。这段回答后的 refresh 不取消 pending chat run，也不改变各入口既有的 `enqueue` 并发语义；
移除回答前 SDK 往返后，模型可更早开始生成。项目不实现 timer、进程队列或 session manager；延时与队列由
Agent Server 管理。SDK 调用只等待 Server 接受请求，不等待提取模型；refresh 异常由原生 error handler 隔离。

静默窗口到期后，独立 Memory Graph 调用 backend 写入；其异常只结束后台 run，第三方原始错误不进入
Assistant state。`RetryPolicy`、error handler
和 `Command` 是 LangGraph 扩展能力，不构成项目自研降级层；
项目只声明 Memory 失败时仍继续回答并显式标记 degraded 这一产品结果。

## 最小 backend 协议

`MemoryBackend` 只有异步 `recall` 与后端写入方法 `commit`；Graph 对外将后者表达为 `memory_extract`。
两者接收 Agent Server 原生 authenticated
`user.identity`、`thread_id/run_id`、标准 messages 和 `runtime.store`。第三方记忆服务只需实现该协议，不需要继承项目 SDK、
创建 session host 或提供通用 CRUD。

当前装配：

- `disabled`：离线默认，召回为空、提取跳过；
- `mem0`：复用薄 `Mem0Client`；身份通过 opaque binding，提交的 `source_turn` 优先使用 Agent Server
  `run_id`，本地 Graph 无 run ID 时使用 thread ID；不构造旧 SQLite commit ledger；
- `langmem`：使用官方 manager，召回访问 runtime Store，后台 manager 只接收当前已完成 turn 的用户正文做
  extract/consolidate，不把 System、AI 或 Tool message 交给提取模型，避免助理生成内容、联网来源、Tool 结果与
  request ID 干扰长期记忆；提取 instruction 只允许稳定用户事实、偏好、长期目标和可复用流程，显式排除天气、新闻、股价、
  日期时间、交通、搜索/Tool 结果，以及助理回答、自我描述、能力限制、知识截止日期和“知识库”措辞；
  非结构化记忆正文默认使用简体中文，代码、协议字段和专有名词可保留原文；
- custom：composition 可注入任何满足 `MemoryBackend` 的第三方 adapter。

mock mode 只能使用 disabled；远端 backend 要求 real mode 和完整显式配置，不能探测 key 后启用，也不能静默
回退。

## 冻结快照与安全

state 只保存有界 `tuple[str, ...]` 与 `ready|empty|degraded`。最多 32 项、每项 4,000 字、总计 12,000 字。
Memory 正文是不可信历史数据。model-call middleware 在最新真实用户请求前临时插入一条独立
`HumanMessage`，用自然语言明确其仅为可能过时的背景资料；该消息只进入本次 Provider 请求，不写入
messages state、checkpoint 或 summarization。Memory 不能覆盖当前请求，也不能用于确认身份、权限、当前事实、
操作参数或 Tool schema。

旧 `MemoryNodeBundle`、commit ledger 与 time-travel Memory 兼容层已随旧 Runtime 删除。Mem0 HTTP client 与
identity adapter 继续由当前最小 `MemoryBackend` 复用；旧 checkpoint 不迁移进 `assistant-native-v1`。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_memory_lifecycle.py \
  tests/tdd/native-memory-service/test_delayed_extraction.py
```
