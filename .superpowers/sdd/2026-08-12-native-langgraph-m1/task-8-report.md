# M1 Task 8 实施报告

## Gap analysis 与 core 决策

- `RUN-001` 需要最小回补：既有 core 只覆盖同步终态，新增原生异步
  `arun_state()` 产生同义 completed 终态和产品事件的证据。
- `LOOP-001` 需要最小回补：既有 core 已覆盖 assistant / governed tool / compose 条件推进；
  只新增每个 Runtime 在多 turn 中复用同一 compiled graph 的稳定证据。
- `IDENT-001` 需要最小回补：新增同一 user/agent/session 的 conversation `thread_id` 稳定、
  不等于 `run_id`、不同 user 隔离，且 runnable config 不伪造 `checkpoint_ns`。
- `OBS-001` 已由 Task 7 core 覆盖 canonical correlation 与 server 不重建 LangSmith OTel tree，不重复增测。
- `GATE-001` 的公共 session/run/frame/terminal 契约未改变，既有 core 保持；production composition
  root 的 native async 选择和不泄漏 graph 内部 event 保留在 M1 临时 TDD，不机械晋升。

## TDD 证据

对三项新 core 分别做 production mutation：async 执行直接返回 created state、每次 sync run
重编译 graph、将 `run_id` 纳入 thread hash。定向用例为 `3 failed`，且分别命中
RUN-001、LOOP-001 和 IDENT-001；恢复实现后为 `3 passed`。

## Authority 同步

- Runtime authority 明确 `Agent-Service <- ProductEventProjector <- astream(v2)` 的逻辑分层、
  每 Runtime 单次编译、runtime context 注入与 graph 内部 event 不进产品协议。
- 身份明确为 stable conversation thread + per-invocation run；M1 root saver disabled，持久
  checkpoint/namespace/subgraph resume/interrupt 归 M2。
- Eval authority 明确 LangSmith Runtime Regression 直接评估真实 graph tree；Langfuse Release
  Review 的等价迁移与退出归 M5。
- Observability authority 在 Task 5/7 已同步 native LangSmith tree 与 canonical audit 的独立职责，
  Task 8 无需制造重复 diff。`docs/authority.toml` 的 route/verification 未改变。

## 离线验证

```text
tests/tdd/native-langgraph-runtime
50 passed

tests/tdd/langsmith-parallel-evaluation tests/tdd/langsmith-evaluator-automation
40 passed

runtime/context/gateway/observability related core
51 passed

default pytest
90 passed

documentation authority validator
valid=true, errors=[]

compileall / git diff --check
passed
```

删除门槛证据：

- `asyncio.to_thread(...run_assistant_request...)`、
  `create_langsmith_experiment_trace_store`、
  `current_langsmith_experiment_binding` 在 `src evals` 零命中。
- 生产 HTTP Gateway 与 Agent-Service 都注入 `run_request_stream()` native async path。
- Runtime 只在构造时创建一个 `AssistantTurnGraphApp`，graph app 只在构造时编译。
- `create_server_trace_store()` 不读 LangSmith config；LangSmith runner 不引用 OTel binding/store。
  仍存在的 `experiment_runtime.py` OTel 边界只供 M1 继续保留的 Langfuse runner。

全部 pytest 使用 mock/local/offline，未读取真实 `.env`，未访问网络或付费服务。

## 真实验收与限制

未获得 operator 对真实 Provider/LangSmith 的明确授权，因此未执行
`--inspect`、`--preflight` 或 `--run`，也未将 fake/mock 结果冒充真实 Experiment 证据。
后续由 operator 按 M1 plan Task 8 Step 6 顺序执行这三条命令，并审核每个 Example
恰有一个 task root、原生 graph/node/LLM/tool 父子树及全部 required Feedback。
因此 M1 当前只能标记为 offline implementation complete / operator acceptance pending，不能声称主 spec
要求的真实 LangSmith evidence 已验收完成。

M1 不承诺持久 checkpoint、interrupt/resume、profile/subgraph 恢复或 Durable Workflow Graph；
分别属于 M2/M3。LangSmith Release Review 等价验收、Langfuse 与兼容 trace/eval 基础设施退出
属于 M5。对主 spec 无未声明偏移；M1 plan Task 1 中的虚拟 `checkpoint_ns` 示例已按主
spec 与 LangGraph 真实 root saver 语义裁决为 M2 延期项。

## 测试策略

Core invariant: RUN-001 / LOOP-001 / IDENT-001 changed because native async terminal parity,
single compiled graph ownership, and stable conversation thread identity are stable framework
contracts. OBS-001/GATE-001 remained covered by their existing core tests.

Tests: updated `tests/core/integration/test_runtime_lifecycle.py`; retained M1 RED/GREEN under
`tests/tdd/native-langgraph-runtime` and `tests/tdd/langsmith-parallel-evaluation`. The user may
delete those temporary feature directories manually; they were not automatically promoted or removed.

## M1 final review fix round 1/5

完整 M1 独立审查发现原生 LangGraph 自动 callback 可能把完整 runtime state 写入 LangSmith；远端 LLM/Tool
projection 对 signed credential/media reference 和异常文本也不够严格；Experiment completeness 只查询最近
一小时；current parent 下 graph metadata/tags 未落到真实 graph run。上述代码 finding 均先由离线 RED 复现。

修复后，在本机 `langchain-core 1.4.3` 验证且受运行时 signature guard 保护的显式 payload-safe tracer 继承
Experiment task parent/client/order map，只清空 graph/node chain inputs/outputs 并安全化 chain error；Dataset
task root input/output、LLM/Tool child 安全投影和唯一真实父子树保留。私有 API 或 tracer 构造失败时，本次
graph scope 原子关闭远端 tracing，业务 graph 继续执行，不允许 ambient auto tracer 接管。远端 redactor
独立于本地 content 开关：普通网页 URL 和普通语义文本保留，signed/auth/cookie/token URL、媒体 URL、
artifact/file/data URI、绝对/相对媒体路径与原始业务异常不进入 LangSmith。completeness 改为按 project_id
全量分页，不再使用一小时窗口。

Fix round fresh offline evidence：native TDD 58 passed；LangSmith eval TDD 40 passed；related core 63 passed；
default core 90 passed。真实 LangSmith/operator acceptance 仍未授权、未执行。

## M1 final review fix round 2/5

第二轮审查补齐远端文本任意位置的 artifact/file/data URI、Windows/相对媒体路径、HTTP userinfo、
`sig`/signed/signature/token/credential 与任意 `X-Amz-*` query；普通 article URL 与自然语言仍保留。
safe tracer、公开 tracing context 和 SDK helper 同时不可用时，第三层 LangSmith tracing ContextVar
只关闭当前 graph scope，保持业务 fail-open 且 fake client 零 graph create/update；只有 ContextVar
本身也不可用时才安全 fail-closed。项目没有 pin `langchain-core 1.4.3`，报告只声明本机版本验证与
运行时 signature guard，不再把环境事实描述为可复现依赖锁定。

Round 2 fresh offline evidence：native TDD 58 passed；LangSmith eval TDD 40 passed；related core
63 passed；default core 90 passed；authority validator、compileall、diff 与删除门槛通过。真实
LangSmith/operator acceptance 仍未授权、未执行。
