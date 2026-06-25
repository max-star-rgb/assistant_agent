# AGENTS.md

本文件是给 Codex / coding agent 的仓库级指导。保持简短、稳定、可自动加载。详细设计在 `docs/`，可执行任务在 `tasks/`。

## 1. 项目目标

构建一个多模态自主工具调用 Agent。Agent 能理解用户的文本、图片、视频、语音等输入，识别用户真实意图，并自主决定使用以下能力：

- LLM 内生能力：对话、推理、总结、结构化输出、代码/文本生成。
- VLM / Video MLLM：图片理解、视频抽帧理解、OCR、场景/物体/动作识别。
- 外部工具：商品搜索、商品比价、图片生成、3D 渲染、记忆检索、数据库查询、消息通知。
- 记忆系统：会话记忆、视频记忆、商品记忆、用户偏好、任务状态。

核心原则：Agent 负责任务编排、意图识别、工具选择、结果融合；具体能力由独立服务或适配器实现。

## 2. 当前推荐技术栈

默认按 Python 后端实现。除非任务文件另有要求，不要引入重型依赖。

- API：FastAPI
- 状态编排：当前包含 LangGraph 运行时与 ReAct assistant loop；轻量状态机/本地 mock 路径保留用于离线测试。
- 数据模型：Pydantic
- 测试：pytest
- 服务通信：HTTP；实时状态后续使用 WebSocket
- 长任务：先用本地任务状态模拟；后续接 Redis / Celery / MQ
- 存储：开发阶段先用本地文件和 SQLite；后续接 PostgreSQL、对象存储、向量库

## 2.1 本地 Python 环境

本仓库默认使用 conda 环境 `hello_agent`。Codex 执行 Python/pytest/脚本时，优先直接调用该环境解释器，避免每条命令使用 `conda run` 的额外开销：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python
```

常用命令示例：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
# 先起后端服务器（owns provider/env 配置）：
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py --provider mock --image-provider mock
# 再用 CLI 客户端连它：
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_client.py --server http://127.0.0.1:8000 "你好"
```

只有在需要执行非 Python 命令且依赖 conda 激活环境变量时，才使用：

```bash
conda run -n hello_agent <command>
```

## 3. 仓库目标结构

```text
repo-root/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── src/
│   └── multimodal_agent/
│       ├── api/
│       ├── agent/
│       ├── schemas/
│       ├── tools/
│       ├── memory/
│       ├── services/
│       └── utils/
├── tests/
├── docs/
├── tasks/
└── prompts/
```

## 4. Codex 阅读规则

在任何实现前：

1. 先读当前阶段的文档入口：Phase 1-7 读 `docs/phase1-7/00-doc-map.md`；Phase 8 读 `docs/phase8/README.md`。
2. 再读当前任务文件，例如 `tasks/phase1-7/003-intent-router.md` 或 `tasks/phase8/phase8A_2_react_final_answer_handoff.md`。
3. 根据任务文件的 `Read first` 只打开必要文档。
4. 涉及架构分层、模块归属、治理边界或重构判断时，读取 `docs/architecture-layers.md`，并保持 Agent/tool/provider/memory 调用不绕过本地 validator、executor、policy、audit 边界。
5. 开发好单库相关功能时，先读取 `haodanku-openapi-docs/AI使用说明.md` 和 `haodanku-openapi-docs/接口目录.md`，按意图路由到对应分类文档再编码。
6. 不要一次性读取所有文档。
7. 不要跨任务提前实现未来阶段。

## 5. 开发顺序

历史 Phase 1-7 任务按 `tasks/phase1-7/README.md` 的顺序推进；当前 Phase 8 任务按 `tasks/phase8/README.md` 和具体任务文件推进。不要跨阶段提前实现未来能力。

1. 项目骨架
2. 领域 Schema
3. AgentState
4. 意图识别与路由
5. Tool Registry
6. 记忆服务
7. 图片/视频理解适配器
8. 商品搜索与比价
9. 图片生成适配器
10. 3D 渲染适配器
11. FastAPI Gateway
12. WebSocket 事件
13. 观测与工具调用日志
14. 端到端 Demo

## 6. 编码约定

- 所有新代码优先放入 `src/multimodal_agent/`。
- 所有公共数据结构使用 Pydantic model。
- 工具调用结果必须结构化，不允许只返回散乱字符串。
- 外部模型/API 先写 adapter interface 和 mock implementation，不要一开始绑定具体供应商。
- Phase 8 assistant loop 的长期方向是真实 LLM 自主决策回复/追问/工具调用；不要再让真实 LLM 路径依赖 intent/router/plan 来选择工具。
- mock/offline 路径只作为稳定测试与本地演示兼容层，后续代码应逐步减少 mock 专用决策逻辑，不要把 mock 行为伪装成真实 LLM 能力。
- 后续新增核心 ReAct/assistant loop 测试应优先覆盖非 mock LLM 决策路径（例如 scripted/fake real chat adapter，`provider != "mock"`），不要默认用 mock rule plan 证明新 Agent 行为；真实外部 LLM/provider 网络调用只放在显式 opt-in 的 smoke/integration 测试中。
- 工具执行失败必须返回可解释错误，不能直接抛出未处理异常给 Agent。
- 所有核心逻辑必须有单元测试。
- 修改行为时同步更新对应 `docs/` 或 `tasks/`。
- 导入规范与包结构规范
- 业务功能建议从具体模块导入，不建议通过 __init__.py 聚合导出。业务对象优先具体模块导入；只有明确的包级公共入口才放进 __init__.py。

## 7. Done Definition

一个任务完成必须满足：

- 任务文件中的 Acceptance 全部通过。
- 新增/修改代码有测试覆盖。
- `pytest` 能通过，或明确说明当前失败原因与未完成依赖。
- 没有把 mock 能力伪装成真实生产能力。
- 总结本次改动、测试结果、下一步建议。

## 8. 不要做的事

- 不要在没有任务要求时接入真实付费 API。
- 不要把密钥写入代码或文档。
- 不要一次实现全部系统。
- 不要删除用户已有代码，除非先说明原因并获得确认。
- 不要把超长架构说明塞进 `AGENTS.md`。


## 9. 每次任务的工作模式

开始时：

```text
我将执行 tasks/xxx.md。
我会阅读：...
计划：...
```

结束时：

```text
完成内容：...
测试结果：...
未完成/限制：...
下一任务：...
```

如果用户请求的是小范围修复、脚本调试或文档更新，且没有明确任务文件，可以说明“未绑定具体任务文件”，但仍需遵守阅读规则、测试规则和 Scope 控制。

## 10. Codex 权限规则

Codex 可以在不询问用户的情况下执行以下操作：

- 新建、修改、删除本仓库内的源码文件。
- 新建、修改、删除本仓库内的测试文件。
- 修改本仓库内的文档文件。
- 运行项目内测试、格式化、类型检查、lint 命令。
- 运行 Python 脚本，包括受控的 python -c 单行脚本。
- 在当前 conda 环境内执行项目命令。
- 在实现任务时直接调用 mock adapter 或本地 adapter 接口进行验证。

允许的命令示例：

    /home/lenovo1/miniconda3/envs/hello_agent/bin/python
    /home/lenovo1/miniconda3/envs/hello_agent/bin/python -c "..."
    /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
    /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
    conda run -n hello_agent <command>
    Added
    Edited

约束：

- 手工新建或修改文件时，默认使用 apply_patch。
- python -c 不作为常规写文件方式；只用于仓库内受控机械替换、环境检查或小范围验证。
- 不用 python -c 绕过任务 Scope，不写入密钥，不调用真实付费 API。
- adapter 调用默认使用 mock/local 实现，真实外部服务必须由任务明确要求。
