# Mem0 原生记忆生命周期迁移实施计划

状态：已完成
日期：2026-07-22

## 1. 背景

当前 `assistant_agent` 同时维护本地 `MemoryManager` 写入策略、显式
`memory_save` 工具、completed-run promotion、Mem0 framework adapter 和本地
typed-fact/profile 算法。Mem0 在现有 framework 路径中固定使用
`infer=false`，因此只承担存储和向量检索，长期记忆的提炼与写入决策仍由主
LLM 和本地 policy 完成。

本次迁移把启用 framework/Mem0 的真实运行路径改成 Mem0 原生生命周期：

- 主 LLM 只消费自动注入的长期记忆，并按需读取每日记录；
- runtime 在成功完成 turn 后，把原生 `user` / `assistant` 消息交给 Mem0；
- Mem0 LLM 使用 `infer=true` 提炼长期记忆；
- 每个完成 turn 另以 `infer=false` 保存一条可检索的 daily record；
- daily 与长期记忆属于同一个 capture 任务的两个存储投影，不是两个 Agent
  工具或两套业务流程。

该文档只记录迁移步骤。实现完成后的架构事实必须同步到根级权威文档。

## 2. 目标架构

```text
Provider-native conversation
  -> load core memories from Mem0
  -> main LLM (memory_search / memory_get are read-only)
  -> completed response
  -> capture_memory(turn)
       -> daily record, infer=false, kind=daily
       -> original messages, infer=true, kind=core
       -> Mem0 LLM extraction + vector persistence
```

### 2.1 长期记忆

- Mem0 是 framework 模式下长期记忆的 lifecycle owner。
- runtime 按当前用户请求检索 `kind=core`，在每个 turn 的主 LLM 调用前注入
  有界结果。
- framework/Mem0 模式不再让本地 `MemoryWritePolicy`、typed-fact conflict
  resolver 或 user-profile projection 决定写入内容。
- Mem0 写入失败不得回退成主模型或本地算法自行提炼；本次记录稳定错误且不
  覆盖用户响应，durable capture 重试/outbox 另行设计。

### 2.2 每日记录

- 物理存储采用“一条完成 turn 对应一条 append-only record”，不维护一个会
  持续变大的每日文档。
- 文本使用适合 embedding 的可读格式：时间、用户请求、助手最终结果；不保存
  system prompt、tool schema、hidden reasoning、raw provider payload 或媒体正文。
- metadata 至少包含 `record_kind=daily`、本地日期、opaque session provenance、turn/run
  provenance 和 schema version。
- daily record 使用跨 session 的 user/project 记忆空间；session 只保留为来源
  metadata，不作为默认检索分区。

### 2.3 主模型工具

正常 Provider tool catalog 只保留两个记忆读取工具：

- `memory_search(query)`：只搜索 `record_kind=daily`，返回候选 ID、内容、时间和来源；
- `memory_get(memory_id)`：按 ID 精确读取一条 daily record。

移除 `memory_save`。系统级 `capture_memory` 是 runtime 内部生命周期调用，不是
模型工具，不进入 `RunToolCatalog`。

## 3. 兼容与边界

- 默认 mock/local/offline 模式继续可启动和完成 run，不调用 Mem0 或其他真实
  Provider。
- 新 capture 生命周期只在显式启用 framework 且 adapter 支持 capture 时执行；
  本地 `memory/jsonl/sqlite` backend 在本阶段保持既有读取能力，但不向主模型
  暴露写入工具。
- Hindsight adapter 保留现有 CRUD/recall 兼容，不获得伪造的 Mem0 inference
  行为。
- `RequestIdentity` 仍是用户隔离来源；跨 adapter 继续使用 opaque identity。
- `MemoryManager` 可暂时保留为读取、上下文预算和 API 兼容 façade，但不再是
  Mem0 framework 写入算法的 owner。后续删除本地旧算法应单独完成，避免本次
  同时破坏离线 backend、API 和数据迁移。

## 4. 实施步骤

1. 扩展 framework adapter/store 契约，增加 completed-turn capture 请求与结果。
2. Mem0 capture 在一个调用边界内完成 daily `infer=false` 和 core
   `infer=true` 写入，并携带可过滤的 `record_kind` metadata。
3. Mem0 recall 支持按 `record_kind` 过滤：自动上下文只查 core，Agent 工具只查 daily。
4. 图尾 `save_memory` 节点改为 capture lifecycle；非 capture backend 明确 no-op。
5. 将 `memory_retrieval` 重命名为 `memory_search`，新增 `memory_get`，移除
   `memory_save` 注册、Provider schema、提示说明和默认目录暴露。
6. framework/Mem0 自动 context load 不再依赖旧的自然语言 read-policy gate；
   仍保留 identity、数量和 prompt-safe context budget。
7. 更新 capability/tool ID、兼容 planner 路径、trace 摘要和相关测试。
8. 同步更新 memory、context、tool-calling 权威文档。

## 5. 验收标准

- Qwen/OpenAI-compatible tools 中只出现 `memory_search` 和 `memory_get`，不出现
  `memory_save` 或通用 `memory` 写入 schema。
- `memory_search` 的模型可见输入只有非空 `query`；`memory_get` 只有非空
  `memory_id`。
- framework/Mem0 成功 turn 会提交一条 daily record 和一次 `infer=true` 原生
  conversation capture。
- daily 搜索跨 session 默认可见，但结果保留 opaque session/turn provenance。
- 自动长期记忆注入只消费 core 结果，不把 daily record 全量塞入主模型上下文。
- mock/local/offline 默认测试不访问网络；framework adapter 使用 fake transport
  验证请求 shape、identity 和 record kind 过滤。
- 现有主文本 run、native tool loop、工具 catalog/validator 和跨用户隔离安全网
  通过。

## 6. 非目标

- 本次不删除所有本地 Memory v2 数据结构、审计 API 或历史迁移代码。
- 本次不引入 DeepSeek 兼容分支。
- 本次不调用真实 Mem0/Qwen Provider 做自动测试。
- 本次不实现每天结束后的第二份聚合摘要；daily turn records 已是可检索时间线。
- 本次不让主 LLM 生成或提交长期记忆正文。

## 7. 实施结果

- Provider tool catalog 已收敛为 `memory_search` 与 `memory_get`，输入分别只有
  `query` 和 `memory_id`。
- runtime 图尾已改为 `capture_memory`；Mem0 daily/core 两次写入相互独立，部分
  失败只进入安全 trace/metadata，不覆盖用户响应。
- core 自动召回和 daily 工具搜索均固定使用跨 session 的 user/project scope，
  session/turn 只作为 opaque provenance。
- 已增加 Mem0 fake transport 协议测试、runtime capture 验收，并更新默认离线安全网。
- 未调用真实 Mem0、Qwen 或其他外部 Provider。
