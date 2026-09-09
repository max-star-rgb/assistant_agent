# assistant_agent

本地优先的多模态助理，统一 `AssistantAgent` 运行在官方 LangGraph Agent Server。
默认 mock；真实 Provider 必须显式配置和启用。

## 新电脑安装

在 WSL2 Ubuntu 中 Git 拉取代码，创建 Python 3.12 虚拟环境后执行
`python -m pip install -r requirements.txt`。下载 BGE、导出 SigLIP2，手动复制旧电脑 `.env` 并核对路径。
使用 `python scripts/run_server.py --no-reload` 启动服务；另一个终端激活环境后运行 `assistant-agent`。

完整步骤、前置条件、模型、数据迁移及故障处理见 [WSL 安装说明](docs/wsl-installation.md)。
此入口迁移现有单机开发服务，不提供公网认证或跨电脑设备执行。

## 工程导航

- [文档导航](docs/README.md)：按主题选择当前 authority。
- [Agent Server](docs/agent-server-architecture.md)：三张原生图、身份与生命周期。
- [Tool 与 MCP](docs/tool-calling-architecture.md)、[Memory](docs/memory-service-architecture.md)、[视觉](docs/visual-perception-architecture.md)。
- [脚本入口](scripts/README.md)、[临时测试规则](tests/README.md)。

源码位于 `src/assistant_agent/`。`native_agent/` 编译统一 Agent 和 middleware，`agent_server/` 装配原生服务，
`tools/` 与 `mcp/` 提供工具，`media/` 拥有独立视觉流水线；`memory/` 是后端适配，`runtime/` 只提供执行资源。
`evaluation/` 保留直接消费原生 Graph 的 target，当前没有正式行为评测 runner。

Git 只承载代码、文档、模板和依赖声明。`.env`、`.local/` 私有配置、`.data/` 模型与运行数据、
`.langgraph_api/` 状态均不跟踪；数据库需单独备份，不能把整个 `.data/` 当可删除缓存。
