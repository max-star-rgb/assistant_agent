# assistant_agent

本地优先的多模态助理，统一 `AssistantAgent` 运行在官方 LangGraph Agent Server。
默认 mock；真实 Provider 必须显式配置和启用。

## 新电脑安装

推荐 **WSL2 Ubuntu 24.04 + RTX 4090**。先准备 Windows NVIDIA 驱动、WSL2 与 Docker Desktop integration，
把代码 clone 到 Ubuntu 的 `~/projects/assistant_agent`，然后运行：

```bash
bash scripts/setup_wsl.sh
bash scripts/start.sh check
bash scripts/start.sh server
```

另一个终端执行 `bash scripts/start.sh client`。模板随 Git 提供，安装不会覆盖已有私有配置；
真实调用前在本机填写 `.env`。默认准备 SigLIP2，不安装 BGE。

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
