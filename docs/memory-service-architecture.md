# Memory Plugin 架构

最后更新：2026-08-08

本文是 `assistant_agent` 长期记忆的当前权威。Runtime 只允许一个排他的 active Memory Plugin，
并且只通过 `MemoryPluginHost` 使用它。Mem0 是默认内置实现；`Mem0Client` 是
`Mem0MemoryPlugin` 的私有 HTTP/service adapter，不再是 Runtime 依赖。

## 1. 边界与所有权

当前调用关系为：

```text
Assistant Runtime / Gateway
  -> LongTermMemoryService（兼容 facade）
  -> MemoryPluginHost（唯一 Runtime 治理边界）
  -> assistant_memory_plugin_v1
  -> active MemoryPlugin（同一 Runtime 恰好一个）
       -> Mem0MemoryPlugin（默认内置）
            -> Mem0Client
            -> Mem0 service

       或

       -> operator 显式配置的可信 Python Memory Plugin
            -> Plugin 私有 client / memory service
```

Memory Plugin 负责记忆算法和私有后端，包括召回、搜索、排序、提取、更新、合并、删除、
embedding、多模态关联以及 Plugin 私有 session/cache。Host 保留不能绕过的治理：

- 从可信 Runtime 身份生成 Plugin-scoped opaque identity；
- API version、slot、配置、factory、descriptor 和返回 schema 校验；
- timeout、取消、重试、并发、有界队列、幂等键和 shutdown；
- owner/session 绑定的媒体读取与 artifact 登记；
- context 安全投影、去重、硬预算、裁剪和 prompt 编译；
- 结构化、脱敏的 trace 与失败降级。

第一版 Plugin 是 operator 显式配置的可信进程内 Python 代码。导入 Plugin 等价于允许它在
Assistant 进程中执行，不是不可信代码沙箱；module import 和 factory build 按契约不应连接远端。
Plugin 返回的 memory text、metadata 和媒体证据仍是不可信历史数据，不能成为
system/developer instruction，也不能覆盖当前用户请求、Runtime policy、ToolSpec、身份或授权。

Memory Plugin API 与 Tool Plugin API 是两条独立边界：

- Memory Plugin 由 Runtime 在固定生命周期调用，不能注册 Tool 或修改 Prompt/AgentState；
- Tool Plugin 贡献 Tool，但所有本地显式调用仍经过
  `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`；
- Memory 不是默认模型可调用 Tool，默认 Registry 不注册 `memory_search`、`memory_get` 或
  `memory_save`，也不新增项目自建的记忆 CRUD/control-plane API。

## 2. 装配、排他 slot 与配置

Memory Plugin API 固定为 `assistant_memory_plugin_v1`，`kind` 固定为 `memory`。Registry 在启动期
先验证完整 inventory，再构造配置选中的唯一 Plugin 并 seal；Host 只持有 sealed Registry。
以下情况全部 fail closed：重复 `plugin_id`、非法 descriptor/API version/kind、未知或禁用 slot、
module/export/factory/config/build 失败，以及活动 Plugin 与登记 descriptor 不一致。显式配置失败时
不会静默回退到 Mem0。

外部 module 必须导出：

```python
__assistant_memory_plugin_factory__
```

入口配置为 `MULTIMODAL_AGENT_MEMORY_PLUGIN_CONFIG_PATH`，文件 schema 示例：

```json
{
  "schema_version": "assistant_memory_plugins_v1",
  "slot": "mem0",
  "plugins": {
    "mem0": {
      "enabled": true,
      "module": "assistant_agent.memory.plugins.builtin.mem0",
      "config": {
        "base_url": "${MEM0_BASE_URL}",
        "api_key": "${MEM0_API_KEY}",
        "timeout_seconds": 5
      }
    }
  }
}
```

配置文件不得保存真实 secret；`${ENV_NAME}` 只由 Host 解析为不回显的 SecretRef。项目不扫描
目录、不使用 Python entry point 自动启用、不因检测到 key 自动切换 Plugin，配置变化重启后生效。
没有提供新配置文件时，composition root 继续从兼容的 `MEM0_BASE_URL`、`MEM0_API_KEY`、
`MEM0_TIMEOUT_SECONDS` 和 `MEM0_IDENTITY_NAMESPACE` 装配默认 `mem0` slot。

Host 执行边界使用：

- `MULTIMODAL_AGENT_MEMORY_PLUGIN_OPEN_TIMEOUT_SECONDS`（默认 `5`）；
- `MULTIMODAL_AGENT_MEMORY_PLUGIN_PREPARE_TIMEOUT_SECONDS`（默认 `5`）；
- `MULTIMODAL_AGENT_MEMORY_PLUGIN_INGEST_TIMEOUT_SECONDS`（默认 `30`）；
- `MULTIMODAL_AGENT_MEMORY_PLUGIN_CLOSE_TIMEOUT_SECONDS`（默认 `5`）；
- `MULTIMODAL_AGENT_MEMORY_INGESTION_MAX_WORKERS`（默认 `2`）；
- `MULTIMODAL_AGENT_MEMORY_INGESTION_MAX_PENDING`（默认 `64`）；
- `MULTIMODAL_AGENT_MEMORY_INGESTION_SHUTDOWN_TIMEOUT_SECONDS`（默认 `10`）；
- `MULTIMODAL_AGENT_MEMORY_SESSION_SNAPSHOT_MAX_ENTRIES`（默认 `1024`）。

`MULTIMODAL_AGENT_PROVIDER_MODE=mock` 时，默认 Mem0 Plugin 使用明确的 unavailable adapter，
不连接网络、不保存或召回记忆。只有 provider mode 为 `real` 且 Mem0 配置完整时，默认 Plugin 才构造
真实 `Mem0Client`；不会从 real 静默回退为一个伪成功的 mock memory backend。

## 3. 四个固定生命周期

Runtime 不广播可修改状态的通用 Memory 事件，而是由 Host 调用四个强类型方法：

```text
open_session -> prepare_context -> ingest_turn -> close_session
```

### 3.1 `open_session`

Session 创建时，Host 生成 `memory_session_id` 和 Plugin-scoped identity，再调用一次
`Plugin.open_session()`。Host 校验并保存 Plugin 私有 `session_handle`，把初始 contribution 冻结为
session baseline；重复初始化由 Host 去重。

`session_handle` 只回传给创建它的同一 Plugin，不进入 Prompt、日志、公开 trace 或 API。Plugin
切换后不会复用旧 handle。打开失败会建立 degraded/unavailable session，冻结空 baseline，不能阻断
session 或后续回答。

### 3.2 `prepare_context`

每个 user turn 在首次模型调用前，Host 最多调用一次 `Plugin.prepare_context()`。Plugin 接收当前
`MemoryMessage`、预算提示和受管 `ManagedMediaRef`，返回本轮完整的结构化 contribution，而不是
增删 patch。Host 将其与 baseline 按 `memory_id` 确定性合并，并在同一个 Agent run 内冻结结果；
后续 ReAct/Tool iteration 只复用这份结果，不重复召回。

如果 Plugin 声明 `supports_context_refresh=false`，Host 不调用该方法，只使用 session baseline。
失败、超时、取消或无效结果统一降级为空的本轮贡献；当前回答继续。

### 3.3 `ingest_turn`

Runtime 形成最终回复并先记录 `response.delivered`，然后把不可变的原始 user/assistant message、
结构化 Tool evidence、受管媒体引用和发生时间交给 Host。Host 生成稳定 idempotency key，并把任务加入
有界后台队列；同一 `user_id + agent_id + session_id` 串行，不同身份可并行。

`ingest_turn()` 的队列满、timeout、Plugin 拒绝或失败只写结构化观测，不把已经完成的 Agent run
改成失败。只有声明 `supports_idempotent_ingestion=true` 的 Plugin 才允许 Host 自动重试。

纯连接级视觉提醒管理 turn 是窄例外：当本轮存在 ToolResult 且全部来自
`visual_reminder_manage` 时，Runtime 根据结构化工具身份以
`reason=connection_scoped_visual_reminder` 跳过整轮 ingestion；判断不读取用户或助手文本。

### 3.4 `close_session`

Session reset、expiry、Gateway 销毁或 Runtime shutdown 时，Host 先停止接收该 session 的新 ingestion，
有界等待已接受任务，再 best-effort 调用幂等的 `Plugin.close_session()`，最后清理 handle、baseline 和
run freeze。关闭不会隐式获得 consolidation 或其他额外写权限；失败只记录清理风险。

## 4. Context 与安全投影

Plugin 只能返回 `MemoryContextContribution` 和 `MemoryContextItem`，不能返回 role message、prompt
patch、绝对路径、凭据、未治理 URL 或 inline Base64。Host 在接受结果前校验 item 数量、总字符、
metadata JSON、ID、时间、relevance、source capability 和媒体 owner。

`ContextBuilder` 从本 run 的冻结 snapshot 读取同一份结构化 items，再执行统一预算和裁剪。
Context renderer 将正文编码为带中文数据边界的 JSON 对象，并明确标记为不可信历史；
`PromptCompiler` 把它放入独立的合成 `user` context message，随后才放当前真实 `user` 请求。
该合成消息不写入 `ConversationStore`，也不再次提交给 Memory Plugin。

固定 system policy 声明记忆可能过期、不完整或错误，当前请求和最新可靠证据优先。Plugin 提供的
budget 只是一项提示，不能扩大 Host 的硬限制。

## 5. 受管多模态引用

文本以外的输入只通过 `ManagedMediaRef` 传递：

```text
ref_id / media_type / mime_type / size_bytes / created_at / owner_scope
```

Plugin 通过构造期注入的 `MemoryMediaReader` 读取已授权 bytes/stream，通过
`MemoryArtifactWriter` 登记新 artifact。Host 校验 owner/session、声明 modality、MIME、有效期、
单项和单 turn 大小、取消与 deadline，并拒绝目录、符号链接和任意路径。Plugin 正常 API 不会获得
绝对路径、`file://`、未治理下载 URL 或 inline Base64。

进程内 Plugin 本身仍是可信代码；受管引用约束的是正式 Host 交互契约和审计边界，不宣称能隔离恶意
Python 代码。

## 6. 默认 Mem0 Plugin

`Mem0MemoryPlugin` 保持既有产品语义：

- `open_session()` 按 Host 生成的不透明 `user_id + agent_id` 调用 Mem0 `get_all`，完整召回跨
  session 长期记忆；Mem0 分页窗口由私有 adapter 展开，不在 client 端固定 `top_k` 截断；
- 它声明 `supports_context_refresh=false`，所以每个 turn 复用 session 创建时的 baseline；
- `ingest_turn()` 把完整 user/assistant messages 和稳定的 opaque `run_id` 交给 Mem0 原生 `add`，
  不设置 `infer=false`；事实提取、合并、更新、向量化和持久化仍由 Mem0 完成；
- `close_session()` 只释放该 Plugin 的进程内幂等记录，不执行 consolidation；
- Mem0 原生响应只在 Plugin 边界转换为标准 context item、change 和 issue。

Host 的 Plugin-scoped identity namespace 保持现有 Mem0 hash 兼容，用户 metadata 不能覆盖身份。
仓库 sidecar 的 `custom_instructions` 继续要求只保留可直接支持、对未来跨 session 有持续价值的事实，
忽略临时视觉环境、短暂状态、凭据、高度敏感信息和未经确认的推断；新提取、合并或更新后的正文使用
简体中文。带时效且值得长期保留的事实仅在日期可靠时使用 `YYYY-MM-DD：` 前缀。

Mem0 私有 HTTP 子集和 adapter 错误语义见
[`memory_server_api_spec.md`](memory_server_api_spec.md)。Runtime 和第三方 Memory Plugin 不依赖该协议。

## 7. 只读诊断 CLI

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m assistant_agent.memory.cli plugins
```

命令输出固定 JSON schema：`active_slot`、`descriptor`、`source`、`selected`、`readiness`、
`issues`、`generation` 和 `sealed`，并带 `schema_version=memory_plugin_assembly_v1`。它只解析配置、
导入显式 module、校验 factory 并装配活动 Plugin；不创建 Host，不调用 `open_session()`、
`prepare_context()`、`ingest_turn()` 或 `close_session()`，也不执行远端健康检查、recall、写入或真实
Provider 请求。

`readiness=ready` 只表示配置和 factory build 已就绪，不保证远端服务健康。默认 mock Mem0 会以
exit code `0` 报告 `sealed=true`、`readiness=unavailable` 和 `memory_plugin_offline`，因为装配有效但
后端按安全模式离线。装配失败以同一 schema 返回 exit code `1`、`sealed=false`、
`generation=null`；报告不包含 Plugin config、解析后的 secret、原始异常或远端响应。
装配期间显式 module、config validator 和 factory 写入的 stdout/stderr 会被丢弃，CLI stdout 只保留
最终 JSON 报告；Plugin 需要诊断构造逻辑时应返回脱敏的结构化 issue，而不是打印配置或异常。

该 CLI 是 Runtime Plugin 的只读装配诊断，不安装、升级、卸载或运行 Plugin 生命周期。

## 8. 观测与失败降级

每次 Host 生命周期调用只记录 prompt-safe 属性：

```text
plugin_id / plugin_version / api_version / memory_session_id
operation / status / latency_ms / item_count / media_count
change_counts / issue_codes / retry_count / timeout
```

普通日志、canonical JSONL 和公开 trace 不记录 memory text、原始 user/assistant message、媒体正文、
session handle、API key、Plugin 原始异常或远端原始响应。Plugin 异常被 Host 转换为稳定的
`memory_plugin_timeout`、`memory_plugin_unavailable`、`memory_plugin_invalid_result` 或
`memory_plugin_internal_error`。

| 生命周期 | 失败行为 |
| --- | --- |
| `open_session` | degraded/unavailable session，空 baseline，继续运行 |
| `prepare_context` | 本 run 使用空的本轮贡献，继续回答 |
| `ingest_turn` | 后台记录失败，不改变已完成 run |
| `close_session` | best effort 清理并记录风险 |
| 媒体读取 | 拒绝不安全引用或结果；其他安全贡献仍按 Host 校验处理 |

现有 `MULTIMODAL_AGENT_LOCAL_MEMORY_TRACE_CONTENT` 只控制本机 loopback Langfuse 的有界正文
overlay。启用时可在 `assistant.turn` 下查看 `memory.turn_ingestion` 的 ADD/UPDATE/DELETE 正文；
canonical event 仍只保留数量、operation 计数和 memory ID。单条 Mem0 演化继续使用其私有 history
API 钻取，Langfuse 派生视图不反写 Memory Plugin。

## 9. Operator Mem0 控制台

本地 Mem0 + Qdrant 运维入口仍为：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_mem0.py
```

它启动并直接管理 Mem0 sidecar 的原生记录，提供 `status`、`list`、`get`、`history`、`add`、
`update`、`delete`、`clear` 和 `exit`。这是可读写的本地 operator 控制台，不是 Runtime Memory
Plugin 管理入口、不是 Assistant 可调用 Tool，也不经过 `assistant_memory_plugin_v1` 生命周期。

控制台变更不会修改已经创建的 session baseline 或本 run 冻结贡献；必须创建新 session 才能在
Assistant 上下文中看到更新。`clear --all` 的确认、持久化与其他安全规则见
[`scripts/README.md`](../scripts/README.md)。

## 10. 代码归属

| 路径 | 职责 |
| --- | --- |
| `memory/plugins/contracts.py` | `assistant_memory_plugin_v1` descriptor、请求、结果与能力契约 |
| `memory/plugins/config.py` | 显式 JSON 配置、SecretRef 和安全错误 |
| `memory/plugins/assembly.py` | module/factory 校验、排他 slot 与原子装配 |
| `memory/plugins/registry.py` | sealed inventory、活动 Plugin、报告和 generation |
| `memory/plugins/host.py` | 身份、四生命周期、冻结、校验、队列、重试、关闭与降级 |
| `memory/plugins/media.py` | owner-bound media store、reader 和 writer |
| `memory/plugins/session_store.py` | Host 私有 handle、baseline、retired state 与并发所有权 |
| `memory/plugins/builtin/mem0.py` | 默认 `Mem0MemoryPlugin` 与 factory；拥有 Mem0 adapter |
| `memory/mem0/` | 仅供默认 Plugin 使用的 Mem0 HTTP、身份与原生模型 |
| `memory/factory.py` | 统一 composition root，装配 Registry、Host 和兼容 facade |
| `memory/service.py` | Runtime 兼容 facade，只委托 `MemoryPluginHost` |
| `memory/cli.py` | 不进入生命周期的只读 Plugin 装配报告 |
| `context/builder.py` | 将本 run 冻结的标准 Memory item 投影进 Assistant context |

## 11. 不提供的能力

项目不提供多 active Memory Plugin 合并、自建检索排序/冲突策略/TTL/promotion、通用 Memory 事件总线、
外部 Plugin RPC、Plugin marketplace 或默认 Memory Tool。第三方服务需要实现受信任的
`assistant_memory_plugin_v1` factory；如果未来需要主动删除、导出、retention 或 consolidation，必须
新增独立、显式且受治理的契约，不能塞进当前四生命周期或绕过 Host。
