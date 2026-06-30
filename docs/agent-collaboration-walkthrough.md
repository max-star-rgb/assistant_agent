# 多 Agent 协作负责人走读

最后更新：2026-06-30

这份文档面向 Agent 开发新手和项目负责人。它用人话解释当前这一阶段已经完成了什么、一次请求怎么流转、主 Agent 和 worker 如何协作、记忆和上下文如何进入 LLM、A2A 用来解决什么问题，以及当前明确不支持什么。

当前详细设计入口仍是 `docs/agent-communication-routing.md`。本文件不是新开发计划，也不是完整接口文档。

## 用户请求怎么流转

- 默认入口还是 `/agent/run`，它走原来的单 Agent 路径，不会自动启用多 Agent。
- 新增的 `/agents/run` 是显式多 Agent 入口，只有用户或调用方明确使用它时才进入多 Agent 路由。
- 请求里的 `user_id`、`session_id` 会先被统一解析，用来绑定用户、会话和追踪记录。
- 系统会记录这次请求选了哪个 Agent、为什么选、有没有出错、有没有委托子任务。
- 默认仍是本地 mock/offline 行为，不会因为机器上有 API key 就自动调用真实模型或外部服务。

## 主 Agent 和 worker 怎么协作

- `agent.default` 是主 Agent，负责接收请求、组织回答、决定是否需要委托。
- `agent.worker` 是本地 worker，负责被主 Agent 调用来完成边界清晰的子任务。
- 委托不是随便发 HTTP 请求，而是通过受控工具 `delegate_to_agent` 进入。
- worker 默认不能再继续委托别的 Agent，避免递归、循环和失控。
- 委托时会带上用户、会话、父 run、trace、预算和深度信息，方便追踪和限制。

## 记忆和上下文怎么进入 LLM

- 每次 Agent 运行前，系统会构造一份给 LLM 的上下文包，而不是把所有历史原样塞进去。
- 短期对话、长期记忆、工具说明、已有工具结果都会按预算筛选和压缩。
- 记忆检索由记忆服务负责，工具只是薄入口，不直接掌管记忆策略。
- 大的工具结果会被摘要或引用化，减少 token 爆炸。
- 子 Agent 不会默认拿到父 Agent 的完整历史，只接收被过滤后的委托上下文。

## A2A 是干什么的

- A2A 可以理解为“让外部 Agent 或系统按一个通用协议来调用本系统”的接口。
- 当前提供 `/.well-known/agent-card.json`，用于公开说明这个 Agent 能做什么、入口在哪。
- 当前提供 `/a2a/rpc`，支持外部用 JSON-RPC 的 `SendMessage` / `message/send` 调用本地网关。
- A2A 只是外部协议适配层，内部运行时仍使用自己的请求、任务、结果结构。
- inbound A2A 已可用；主动调用远程 A2A 只作为显式配置的试点能力，不是默认能力。

## 当前不支持什么

- 不支持公网 Agent 网络或自动发现远程 Agent 后启用。
- 不支持让 LLM 自由选择任意目标 Agent。
- 不支持默认真实 provider 调用，真实模型或外部服务仍必须显式 opt-in。
- 不支持 worker 继续递归委托 worker 或反复来回委托。
- 运行记录目前主要是本进程内记录，重启后不作为长期正式留存。

## 简单流程图

```text
用户 / API / 外部 A2A 调用
        |
        v
入口层
  /agent/run      -> 默认单 Agent
  /agents/run     -> 显式多 Agent
  /a2a/rpc        -> A2A 协议适配
        |
        v
身份与会话解析
  user_id / session_id / trace_id
        |
        v
主 Agent: agent.default
        |
        +--> 构造上下文
        |      短期对话 + 长期记忆 + 工具说明 + 压缩后的工具结果
        |
        +--> 普通工具调用
        |
        +--> delegate_to_agent
                 |
                 v
              worker: agent.worker
                 |
                 v
              子任务结果 / 摘要 / artifact
        |
        v
最终回答 + run_id + trace_id + 可查询的运行摘要
```
