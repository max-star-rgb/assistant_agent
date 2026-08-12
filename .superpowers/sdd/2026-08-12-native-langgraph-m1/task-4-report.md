# M1 Task 4 实施报告

## 结果

- 新增 `run_assistant_request_async()`，共享 Assistant Service 的 conversation、realtime task state、
  artifacts preparation/finalization，仅执行阶段直接 await `AgentGraphRuntime.arun_state()`。
- `run_assistant_request_stream()` 不再通过 `asyncio.to_thread(run_assistant_request, ...)` 运行同步主路径；
  同步 `run_assistant_request()` API 保持兼容。
- `AgentRunStream` 在 owner event loop 中直接发布 event/result；只有同步 LangGraph node 或显式同步兼容
  调用从其他线程进入时才使用 `call_soon_threadsafe()`。
- Gateway 仍消费 `AgentEvent` 与 `AssistantRunArtifacts`，没有暴露 `GraphStreamPart`、checkpoint、task 或
  graph namespace；未改 Gateway wire、session、cancel、interrupt、reconnect 与 shopping detail 契约。
- review fix round 1 后，HTTP Gateway 默认 factory 注入 `GatewayRuntimePool.run_request_stream()`，
  Agent-Service manager 注入 `AssistantRuntimeApp.run_request_stream()`；默认生产 composition roots 不再选择
  adapter 的同步兼容分支。
- Runtime pool 在非阻塞 owner loop 的 executor wait 中 checkout，并把 lease 保持到 inner stream 的
  terminal result/exception；成功、结构化取消和异常路径均在发布 outer terminal 前归还 runtime。
- 同步更新 `docs/runtime-event-stream-architecture.md` 的 production async 主路径和线程模型。

## TDD 证据

RED：

```text
test_service_stream_uses_native_async_runtime_without_thread_bridge
AssertionError: native graph stream must not use asyncio.to_thread

test_agent_run_stream_enqueues_directly_on_its_owner_loop
AssertionError: same-loop publication must enqueue directly

2 failed
```

首轮 GREEN：

```text
tests/tdd/native-langgraph-runtime/test_async_runtime.py
6 passed
```

测试通过真实 `AgentGraphRuntime` lifecycle 与 graph-app async probe 区分 `arun()`/`invoke()`，对按序
`AgentEvent`、response delta、final event 和权威 terminal artifacts 断言；不是对 mock 调用本身断言。

Review fix round 1 的真实 composition-root RED：

```text
test_default_gateway_factory_uses_pool_native_async_stream
result.status: error != completed

test_agent_service_manager_uses_runtime_app_native_async_stream
result.status: error != completed

2 failed
```

修复后 Task 4 TDD 共 `9 passed`，同时覆盖 HTTP response capture、pool terminal lease、异常与取消归还。

## 验证

```text
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/tdd/native-langgraph-runtime/test_async_runtime.py \
  tests/core/contract/test_gateway_contract.py
16 passed

MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py
27 passed

MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q
86 passed

python -m compileall -q \
  src/assistant_agent/runtime/assistant_run_service.py \
  src/assistant_agent/runtime/event_stream.py
通过

python scripts/check_documentation_authority.py --repo-root .
valid=true, errors=[]

git diff --check
通过
```

全部 pytest 使用 mock/offline；未调用真实 Provider、网络或付费服务。

## 测试策略

Core invariant: unchanged。`GATE-001` 的外部协议和 lifecycle 没有变化，既有 Gateway contract 全部通过，
因此未机械修改永久 core 测试。

Tests: 更新 `tests/tdd/native-langgraph-runtime` 做临时 RED/GREEN；用户可手动删除整个目录，不自动晋升
core。

## 限制与后续边界

- 显式注入的同步 `run_request=` hook 仍保留 worker-thread compatibility bridge；HTTP Gateway 与
  Agent-Service 默认 composition roots 均使用 native async service stream。
- sync-only SDK、Tool、memory/filesystem 和 post-response ingestion 的局部线程边界不属于本任务，也没有
  被虚假声明为 async-native。
- 未修改 `AgentGraphRuntime.run_stream()` 兼容 facade；Gateway 默认实际调用链直接进入 Service 的
  native async path，runtime pool 只在线程池中等待其同步 Condition，不在线程内执行 graph。
- 未实施 Task 5 tracing 或 M2 persistence/interrupt。
